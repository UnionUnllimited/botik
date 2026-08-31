"""Раздача обновлений прошивки: хранение образов и сборка манифеста.

Роутеры обновляются сами. Раз в сутки каждый делает один HTTPS GET по адресу,
зашитому в прошивку, разбирает JSON и действует по нему: номер версии выше
своего — качает образ своей модели, сверяет sha256, шьётся. Ни ручек, ни
авторизации, ни отчёта обратно — панель не знает и не может знать, сколько
роутеров обновилось.

Отсюда три требования, которые здесь и выполняются:

  * **sha256 и размер считает сервер.** Ошибка в одном знаке тихо отменяет
    обновление у всего парка: роутер бросает закачку и молчит до завтра.
    Руками эти поля не вводятся нигде.
  * **Считать надо на лету.** Образ весит 27–54 МБ, и `await file.read()`
    целиком в память — это по полсотни мегабайт на каждую загрузку.
  * **Версия строго растёт.** Понижение роутер игнорирует, так что «выпустить
    прежнюю версию заново» невозможно — только новую, с большим номером.

Манифест собирается из базы на каждый запрос, а не лежит файлом: доля раскатки
меняется ползунком и должна применяться сразу, без пересборки.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import FirmwareImage, FirmwareRelease
from core.redis_client import get_redis

log = structlog.get_logger("services.firmware")

URL_PREFIX = "/firmware"
"""Свой префикс, а не общий `/media`: там картинки товаров, которые чистятся
и переписываются, а здесь файлы, которые тянет парк железа."""

IMAGES_PREFIX = f"{URL_PREFIX}/images"
MANIFEST_PATH = f"{URL_PREFIX}/manifest.json"

IMAGE_SUFFIX = "-sysupgrade.bin"
"""Чем обязано оканчиваться имя файла. Проверка не косметическая: `factory.bin`
той же модели весит столько же и выглядит так же, а прошивка им через sysupgrade
превращает роутер в кирпич."""

MODELS: tuple[tuple[str, str], ...] = (
    ("cudy,wr3000e-v1", "Cudy WR3000E"),
    ("cudy,wr3000s-v1", "Cudy WR3000S"),
    ("cudy,tr3000-v1", "Cudy TR3000"),
    ("zbtlink,zbt-z8103ax-c", "ZBT Z8103AX-C"),
)
"""Ключи моделей ровно в том виде, в каком их называет прошивка, вместе
с запятой. Список закрытый, и это защита: ключ вводится не руками, а выбором
строки — опечатка в нём означала бы, что модель молча осталась без обновления.
Новая модель появляется здесь одной строкой."""

MODEL_KEYS: tuple[str, ...] = tuple(key for key, _title in MODELS)
MODEL_TITLES: dict[str, str] = dict(MODELS)

ROLLOUT_STEPS: tuple[int, ...] = (0, 5, 25, 50, 100)
"""Значения ползунка. Промежуточных нет намеренно: доля выбирается на глаз,
а «37 %» ничем не лучше «25 %» и лишь добавляет способ промахнуться."""

ROLLOUT_WARNING = (
    "Возврат в 0 останавливает раздачу новым роутерам, но уже обновившихся "
    "не откатывает: роутер ставит только версии выше своей."
)
"""Написано рядом с ползунком. Единственное, что тут можно сделать неправильно, —
решить, что нулём обновление отменяется целиком."""

TICKET_TTL_SEC = 15 * 60
"""Столько живёт билет на загрузку. Образ в 54 МБ по обычному каналу уезжает
несколько минут, и двух минут, как у билета на панель, здесь не хватит."""

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_BUILD_IN_NAME = re.compile(r"titan-r(\d{1,9})", re.IGNORECASE)
"""Номер сборки в имени образа: `titan-r140-cudy-wr3000e-v1-…-sysupgrade.bin`.

Его сверка с полем «Версия» — единственная проверка, которая ловит ошибку,
кладущую весь парк молча. Объяви выпуск версией 140, приложи к нему образы
сборки 139 — и роутер поставит 139, назовётся 139 и назавтра снова увидит
в манифесте 140. Так весь парк уходит в круг перепрошивок, и наружу это
никак не видно: устройства о себе не сообщают ничего.

Ловится глазами ровно один раз, и то если посмотреть. Дальше — только этой
регуляркой."""


class FirmwareError(RuntimeError):
    """Причина отказа с текстом для формы."""


def build_number(file_name: str) -> int | None:
    """Номер сборки из имени файла. `None` — в имени его нет.

    Отсутствие номера не отказ: имя могли поменять руками, а сравнивать
    нам тогда не с чем. Расхождение — отказ.
    """
    match = _BUILD_IN_NAME.search(file_name or "")
    return int(match.group(1)) if match else None


def name_mismatch(file_name: str, version: int) -> bool:
    found = build_number(file_name)
    return found is not None and found != version


class Chunked(Protocol):
    """Что угодно, что отдаёт файл кусками: `UploadFile` подходит как есть."""

    async def read(self, size: int) -> bytes: ...


# ── Где лежат файлы ──────────────────────────────────────────────────────────


def firmware_root() -> Path:
    return Path(settings.app.media_dir) / "firmware"


def images_root() -> Path:
    return firmware_root() / "images"


def _release_dir(version: int) -> Path:
    return images_root() / f"v{version}"


def safe_file_name(name: str) -> str:
    """Имя файла без пути и без сюрпризов.

    Имя приходит из формы, то есть от человека, а ляжет оно на диск и уедет
    в URL. Берём последний сегмент и вычищаем всё, кроме букв, цифр, точки,
    дефиса и подчёркивания.
    """
    tail = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _SAFE_NAME.sub("_", tail).lstrip(".")
    return cleaned[:180]


# ── Приём образа ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SavedImage:
    file_name: str
    url_path: str
    sha256: str
    size_bytes: int


CHUNK = 1024 * 1024


async def save_upload(*, version: int, model_key: str, file_name: str, source: Chunked) -> SavedImage:
    """Кладёт образ на диск, считая sha256 и размер по дороге.

    Пишем во временный файл рядом и переименовываем: за образом приходит парк,
    и запись на месте отдала бы кому-то половину файла. Хеш считается тем же
    проходом — второй проход по 54 МБ ради того же числа не нужен.
    """
    if model_key not in MODEL_TITLES:
        raise FirmwareError("Неизвестная модель роутера.")

    name = safe_file_name(file_name)
    if not name.endswith(IMAGE_SUFFIX):
        raise FirmwareError(f"Имя файла должно оканчиваться на «{IMAGE_SUFFIX}».")

    # Отказ здесь, а не при публикации: это самый ранний момент, когда ошибку
    # ещё видно целиком — файл в руках, номер в имени и объявленная версия
    # рядом. При публикации остаётся только список имён.
    found = build_number(name)
    if found is not None and found != version:
        raise FirmwareError(
            f"В имени файла сборка r{found}, а выпуск объявлен версией {version}. "
            f"Либо это образ не от той сборки, либо выпуск надо было заводить "
            f"версией {found}: удалите черновик и создайте заново. Разойдись эти "
            "числа — роутер поставит одну прошивку, назовётся другой версией "
            "и назавтра начнёт всё сначала, и так весь парк."
        )

    directory = _release_dir(version)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("firmware.mkdir_failed", error=str(exc), path=str(directory))
        raise FirmwareError("Не удалось создать каталог для образов.") from exc

    target = directory / name
    tmp = directory / f".{name}.{secrets.token_hex(4)}.part"
    digest = hashlib.sha256()
    size = 0
    limit = settings.app.firmware_max_bytes

    try:
        with tmp.open("wb") as out:
            while chunk := await source.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise FirmwareError(f"Файл больше {limit // (1024 * 1024)} МБ.")
                digest.update(chunk)
                out.write(chunk)
        if not size:
            raise FirmwareError("Файл пустой.")
        tmp.replace(target)
    except FirmwareError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        log.warning("firmware.save_failed", error=str(exc), path=str(target))
        raise FirmwareError("Не удалось сохранить файл на диск.") from exc

    saved = SavedImage(
        file_name=name,
        url_path=f"{IMAGES_PREFIX}/v{version}/{name}",
        sha256=digest.hexdigest(),
        size_bytes=size,
    )
    log.info(
        "firmware.image_saved",
        version=version,
        model=model_key,
        name=name,
        bytes=size,
        sha256=saved.sha256,
    )
    return saved


def delete_file(url_path: str | None) -> None:
    """Убирает файл образа. Молча: запись в базе уже снята, а лишний файл
    на диске не мешает никому, кроме нас."""
    if not url_path or not url_path.startswith(f"{IMAGES_PREFIX}/"):
        return
    tail = url_path.removeprefix(f"{IMAGES_PREFIX}/")
    parts = [part for part in tail.split("/") if part]
    if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
        return
    try:
        (images_root() / parts[0] / parts[1]).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("firmware.delete_failed", path=url_path, error=str(exc))


def delete_release_dir(version: int) -> None:
    directory = _release_dir(version)
    try:
        if not directory.is_dir():
            return
        for item in directory.iterdir():
            item.unlink(missing_ok=True)
        directory.rmdir()
    except OSError as exc:
        log.warning("firmware.rmdir_failed", path=str(directory), error=str(exc))


# ── Выпуски ──────────────────────────────────────────────────────────────────


async def max_version(session: AsyncSession) -> int:
    """Наибольший заведённый номер — черновики считаются тоже.

    Иначе черновик 141 и опубликованный 140 позволили бы завести второй
    черновик 141, а `version` в таблице уникален: отказ вылез бы при
    сохранении, а не при вводе.
    """
    return int(await session.scalar(select(func.coalesce(func.max(FirmwareRelease.version), 0))) or 0)


async def next_version(session: AsyncSession) -> int:
    return await max_version(session) + 1


async def current_release(session: AsyncSession) -> FirmwareRelease | None:
    """Что раздаётся сейчас: опубликованный выпуск с наибольшим номером."""
    return await session.scalar(
        select(FirmwareRelease)
        .where(FirmwareRelease.published_at.is_not(None))
        .order_by(FirmwareRelease.version.desc())
        .limit(1)
    )


async def releases(session: AsyncSession, limit: int = 50) -> list[FirmwareRelease]:
    result = await session.scalars(
        select(FirmwareRelease).order_by(FirmwareRelease.version.desc()).limit(limit)
    )
    return list(result)


async def get_release(session: AsyncSession, release_id: int) -> FirmwareRelease | None:
    return await session.get(FirmwareRelease, release_id)


def normalize_rollout(value: Any) -> int:
    """Ближайшее значение ползунка. Пришедшее мимо шагов не отвергаем:
    отказ на середине экстренной остановки хуже, чем округление."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    number = max(0, min(100, number))
    return min(ROLLOUT_STEPS, key=lambda step: (abs(step - number), step))


async def create_release(
    session: AsyncSession, *, version: Any, notes: str, author: str = ""
) -> FirmwareRelease:
    """Заводит черновик. Номер проверяется здесь, а не в форме: форма — чужой
    процесс, и разъехавшись, она пропустит то, что база потом не примет."""
    try:
        number = int(str(version).strip())
    except (TypeError, ValueError) as exc:
        raise FirmwareError("Версия — целое число.") from exc

    if number <= 0:
        raise FirmwareError("Версия — положительное целое число.")

    previous = await max_version(session)
    if number <= previous:
        raise FirmwareError(
            f"Версия должна быть больше предыдущей ({previous}): роутер игнорирует "
            "номер ниже своего, и выпуск не доедет ни до кого."
        )

    # Черновик на странице показывается один — тот, что новее. Заведи мы второй,
    # образы, загруженные в первый, стали бы невидимы, а места на диске занимали
    # бы по 216 МБ на выпуск.
    open_draft = await session.scalar(
        select(FirmwareRelease)
        .where(FirmwareRelease.published_at.is_(None))
        .order_by(FirmwareRelease.version.desc())
        .limit(1)
    )
    if open_draft is not None:
        raise FirmwareError(
            f"Черновик {open_draft.version} ещё не опубликован — "
            "опубликуйте или удалите его."
        )

    release = FirmwareRelease(
        version=number,
        notes=(notes or "").strip()[:500],
        rollout=0,
        rollout_max=0,
        created_by=(author or "").strip()[:120],
        # Пустой список сразу: после `flush` объект становится сохранённым,
        # и обращение к незаполненной связи ушло бы в базу синхронно — под
        # async-движком это падение, а не запрос.
        images=[],
    )
    session.add(release)
    await session.flush()
    log.info("firmware.release_created", version=number, author=author)
    return release


async def set_rollout(session: AsyncSession, release: FirmwareRelease, value: Any) -> int:
    """Меняет долю парка. Применяется сразу: манифест собирается из базы."""
    rollout = normalize_rollout(value)
    release.rollout = rollout
    if release.published_at is not None:
        release.rollout_max = max(release.rollout_max, rollout)
    await session.flush()
    log.info("firmware.rollout_set", version=release.version, rollout=rollout)
    return rollout


async def publish(session: AsyncSession, release: FirmwareRelease, *, rollout: Any = 0) -> None:
    """Публикует выпуск: с этой секунды он и есть манифест.

    Без образов публиковать нечего: манифест с пустым `images` роутеры прочтут
    и ничего не сделают, зато номер версии уедет наверх и следующий выпуск
    придётся называть большим числом.
    """
    if not release.images:
        raise FirmwareError("Сначала загрузите хотя бы один образ.")

    # Вторая застава на том же месте: приём образа мимо этой проверки уже
    # не пройдёт, но в базе лежат выпуски, заведённые до неё, — а публикация
    # и есть тот рубеж, после которого исправлять нечего.
    wrong = [image.file_name for image in release.images if name_mismatch(image.file_name, release.version)]
    if wrong:
        raise FirmwareError(
            f"Версия выпуска {release.version}, а в именах образов другая сборка: "
            f"{', '.join(sorted(wrong))}. Публиковать нельзя: роутер поставит одну "
            "прошивку, назовётся другой версией и назавтра начнёт всё сначала."
        )

    if release.published_at is None:
        release.published_at = dt.datetime.now(dt.UTC)
    release.rollout = normalize_rollout(rollout)
    release.rollout_max = max(release.rollout_max, release.rollout)
    await session.flush()
    log.info(
        "firmware.published",
        version=release.version,
        rollout=release.rollout,
        images=len(release.images),
    )


async def delete_release(session: AsyncSession, release: FirmwareRelease) -> None:
    """Убирает выпуск вместе с файлами. Раздающийся сейчас не отдаём:
    роутеры в эту минуту качают по этим ссылкам."""
    current = await current_release(session)
    if current is not None and current.id == release.id:
        raise FirmwareError(
            "Это раздаваемый сейчас выпуск. Остановите раздачу и выпустите новый: "
            "удалять то, что качают роутеры, нельзя."
        )
    version = release.version
    await session.delete(release)
    await session.flush()
    delete_release_dir(version)
    log.info("firmware.release_deleted", version=version)


async def attach_image(
    session: AsyncSession, release: FirmwareRelease, *, model_key: str, saved: SavedImage
) -> FirmwareImage:
    """Кладёт образ в выпуск, заменяя прежний для этой же модели."""
    existing = next((item for item in release.images if item.model_key == model_key), None)
    if existing is not None:
        if existing.url_path != saved.url_path:
            delete_file(existing.url_path)
        release.images.remove(existing)
        await session.flush()

    image = FirmwareImage(
        release_id=release.id,
        model_key=model_key,
        file_name=saved.file_name,
        url_path=saved.url_path,
        sha256=saved.sha256,
        size_bytes=saved.size_bytes,
    )
    session.add(image)
    await session.flush()
    await session.refresh(release, ["images"])
    return image


async def detach_image(session: AsyncSession, release: FirmwareRelease, model_key: str) -> bool:
    """Убирает модель из выпуска — штатный способ приостановить её одну:
    модели нет в `images`, и роутеры этой модели ничего не делают."""
    image = next((item for item in release.images if item.model_key == model_key), None)
    if image is None:
        return False
    delete_file(image.url_path)
    release.images.remove(image)
    await session.flush()
    log.info("firmware.image_detached", version=release.version, model=model_key)
    return True


# ── Манифест ─────────────────────────────────────────────────────────────────


def _absolute(url_path: str) -> str:
    return f"{settings.api.public_base_url.rstrip('/')}{url_path}"


def _admin_absolute(url_path: str) -> str:
    """Адрес для ссылок, которые открывает браузер оператора.

    Отдельно от публичного намеренно. Публичный ведёт на витрину, а витрина
    у нас за прокси на другом континенте — гонять через него стомегабайтный
    образ прошивки незачем: оператор и приложение стоят рядом. Тем же
    адресом открывается веб-панель роутера (`fleet_api`), и по той же причине.
    """
    return f"{settings.api.admin_base_url.rstrip('/')}{url_path}"


def manifest_url() -> str:
    return _absolute(MANIFEST_PATH)


EMPTY_MANIFEST: dict[str, Any] = {"version": 0, "rollout": 0, "images": {}}
"""Что отдаётся, пока не опубликовано ничего. Пустой `images` роутер прочтёт
и ничего не сделает; 404 он вправе понять как «адрес сменился». Разницы
для устройства никакой, а разбираться с этим у клиента дома куда сложнее,
чем посмотреть сюда."""


def manifest_of(release: FirmwareRelease | None) -> dict[str, Any]:
    if release is None:
        return dict(EMPTY_MANIFEST)
    body: dict[str, Any] = {"version": release.version}
    if release.notes:
        body["notes"] = release.notes
    body["rollout"] = release.rollout
    body["images"] = {
        image.model_key: {
            "url": _absolute(image.url_path),
            "sha256": image.sha256,
            "size": image.size_bytes,
        }
        for image in sorted(release.images, key=lambda item: item.model_key)
    }
    return body


async def manifest(session: AsyncSession) -> dict[str, Any]:
    return manifest_of(await current_release(session))


# ── Билет на загрузку ────────────────────────────────────────────────────────
#
# Образ уезжает из браузера оператора прямо к нам, минуя админку бота: она
# отдельный процесс на хосте, и 54 МБ через неё — это лишний перегон, лишняя
# память и второй таймаут. Но общий токен ей в браузер отдавать нельзя, поэтому
# приём закрыт разовым билетом — тем же приёмом, что вход в панель роутера.


@dataclass(slots=True)
class UploadTarget:
    release_id: int
    model_key: str


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.security.secret_key.get_secret_value(), salt="firmware-upload")


def _ticket_key(ticket_id: str) -> str:
    return settings.redis.key("firmware_upload", ticket_id)


async def issue_ticket(*, release_id: int, model_key: str) -> str:
    """Выдаёт билет на приём одного файла. Возвращает готовый адрес формы."""
    if model_key not in MODEL_TITLES:
        raise FirmwareError("Неизвестная модель роутера.")
    ticket_id = secrets.token_urlsafe(32)
    target = UploadTarget(release_id=release_id, model_key=model_key)
    await get_redis().set(_ticket_key(ticket_id), json.dumps(asdict(target)), ex=TICKET_TTL_SEC)
    log.info("firmware.ticket_issued", release_id=release_id, model=model_key)
    return f"{_admin_absolute(URL_PREFIX)}/upload?ticket={_serializer().dumps(ticket_id)}"


async def redeem_ticket(raw_ticket: str) -> UploadTarget | None:
    """Гасит билет. Второй загрузки по той же ссылке не будет."""
    if not raw_ticket:
        return None
    try:
        ticket_id = _serializer().loads(raw_ticket)
    except BadSignature:
        log.warning("firmware.ticket_bad_signature")
        return None
    stored = await get_redis().getdel(_ticket_key(ticket_id))
    if stored is None:
        return None
    try:
        return UploadTarget(**json.loads(stored))
    except (TypeError, ValueError):
        return None
