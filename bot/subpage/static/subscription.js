// Функция для установки темы
    function setTheme(themeName) {
        localStorage.setItem('subscription_theme', themeName);
        document.documentElement.className = themeName;
        // Обновляем цвета Telegram Web App
        try {
            const tg = window.Telegram?.WebApp;
            if (tg) {
                if (themeName === 'theme-dark') {
                    tg.setHeaderColor('#000000');
                    tg.setBackgroundColor('#000000');
                } else {
                    tg.setHeaderColor('#F2F2F7');
                    tg.setBackgroundColor('#F2F2F7');
                }
            }
        } catch (e) {
            console.log("Не удалось обновить цвета Telegram Web App:", e);
        }
    }

    // Функция для переключения темы
    function toggleTheme() {
        const switcher = document.getElementById('theme-switcher-grid');
        if (switcher) {
            switcher.classList.toggle('night-theme');
            const isDark = switcher.classList.contains('night-theme');
            setTheme(isDark ? 'theme-dark' : 'theme-light');
        }
        // Тактильная обратная связь
        try {
            if (window.Telegram?.WebApp?.HapticFeedback?.impactOccurred) {
                window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
            }
        } catch (e) {}
    }

    // Устанавливаем тему при загрузке
    (function() {
        const savedTheme = localStorage.getItem('subscription_theme') || 'theme-dark';
        document.documentElement.className = savedTheme;
    })();


    // --- Умные уведомления (Toasts) ---
    function showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        let icon = 'ℹ️';
        if (type === 'success') icon = '✅';
        if (type === 'error') icon = '❌';
        
        toast.innerHTML = `<span class="toast-icon">${icon}</span><span>${message}</span>`;
        
        container.appendChild(toast);
        
        // Анимация появления
        requestAnimationFrame(() => {
            toast.classList.add('show');
        });
        
        // Виброотклик
        try {
            if (window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred) {
                const impact = type === 'success' ? 'success' : (type === 'error' ? 'error' : 'light');
                window.Telegram.WebApp.HapticFeedback.notificationOccurred(impact);
            }
        } catch (e) {}

        // Удаление
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    // --- Конфетти ---
    function fireConfetti() {
        const canvas = document.getElementById('confetti-canvas');
        const ctx = canvas.getContext('2d');
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        
        const particles = [];
        const particleCount = 100;
        const colors = ['#007AFF', '#34C759', '#FF9500', '#FF2D55', '#AF52DE'];
        
        for (let i = 0; i < particleCount; i++) {
            particles.push({
                x: canvas.width / 2,
                y: canvas.height / 2,
                vx: (Math.random() - 0.5) * 10,
                vy: (Math.random() - 0.5) * 10 - 5,
                size: Math.random() * 5 + 2,
                color: colors[Math.floor(Math.random() * colors.length)],
                life: 100
            });
        }
        
        function animate() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            let active = false;
            
            particles.forEach(p => {
                if (p.life > 0) {
                    active = true;
                    p.x += p.vx;
                    p.y += p.vy;
                    p.vy += 0.2; // gravity
                    p.life--;
                    
                    ctx.fillStyle = p.color;
                    ctx.beginPath();
                    ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                    ctx.fill();
                }
            });
            
            if (active) requestAnimationFrame(animate);
            else ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
        
        animate();
    }

    // --- Определение устройства ---
    function detectDevice() {
        const ua = navigator.userAgent.toLowerCase();
        if (ua.includes('iphone') || ua.includes('ipad') || ua.includes('ipod')) return 'ios';
        if (ua.includes('android')) return 'android';
        if (ua.includes('windows')) return 'windows';
        if (ua.includes('mac os')) return 'macos';
        return 'unknown';
    }

    // --- Туториал ---
    // Глобальные переменные для управления туториалом
    let tutorialTimers = [];
    let tutorialCancelled = false;
    let typeTextTimer = null;
    
    function clearAllTutorialTimers() {
        tutorialTimers.forEach(timer => clearTimeout(timer));
        tutorialTimers = [];
        if (typeTextTimer) {
            clearTimeout(typeTextTimer);
            typeTextTimer = null;
        }
    }
    
    function typeText(element, text, speed = 50) {
        return new Promise(resolve => {
            // Сбрасываем флаг отмены
            tutorialCancelled = false;
            element.textContent = '';
            element.style.opacity = '1';
            element.classList.remove('completed'); // Убираем класс completed для показа курсора
            let i = 0;
            function type() {
                // Проверяем, не был ли туториал отменен
                if (tutorialCancelled) {
                    element.classList.add('completed');
                    resolve();
                    return;
                }
                if (i < text.length) {
                    // Добавляем символ
                    element.textContent += text.charAt(i);
                    i++;
                    // Продолжаем печатать
                    typeTextTimer = setTimeout(type, speed);
            } else {
                    // Убираем курсор после завершения
                    const timer = setTimeout(() => {
                        if (!tutorialCancelled) {
                            element.classList.add('completed');
                            resolve();
                        }
                    }, 500);
                    tutorialTimers.push(timer);
                }
            }
            type();
        });
    }

    function startTutorial() {
        console.log('=== START TUTORIAL ===');
        
        // Очищаем предыдущие таймеры и сбрасываем флаг отмены
        clearAllTutorialTimers();
        tutorialCancelled = false;
        
        const overlay = document.getElementById('tutorial-overlay');
        const textEl = document.getElementById('tutorial-text');
        const arrowEl = overlay?.querySelector('.tutorial-arrow');
        const step1Target = document.getElementById('tutorial-step-1-target');
        const step2Target = document.getElementById('tutorial-step-2-target');
        const modalNextBtn = document.getElementById('next-step-btn');
        const modalPrevBtn = document.getElementById('prev-step-btn');
        const modal = document.getElementById('connection-guide-modal');
        const modalSheet = modal?.querySelector('.modal-sheet');
        
        console.log('Elements check:', {
            overlay: !!overlay,
            textEl: !!textEl,
            arrowEl: !!arrowEl,
            step1Target: !!step1Target,
            step2Target: !!step2Target,
            modal: !!modal,
            modalSheet: !!modalSheet
        });
        
        if (!overlay || !textEl || !step1Target || !step2Target) {
            console.error('❌ Tutorial elements not found!');
            return;
        }
        
        console.log('✅ All elements found');

        // Блокируем все клики в модалке
        if (modal) {
            modal.style.pointerEvents = 'none';
            console.log('Modal pointer-events disabled');
        }
        
        // Сбрасываем состояние
        textEl.textContent = '';
        if (arrowEl) {
            arrowEl.style.display = 'block';
            arrowEl.style.visibility = 'visible';
            arrowEl.style.opacity = '1';
            console.log('Arrow element prepared');
        }
        
        // Убеждаемся, что мы на шаге 1
        const step1Panel = document.getElementById('step-1');
        const step2Panel = document.getElementById('step-2');
        if (step1Panel && step2Panel) {
            step1Panel.classList.add('active');
            step2Panel.classList.remove('active');
            if (modalPrevBtn) modalPrevBtn.disabled = true;
            if (modalNextBtn) modalNextBtn.disabled = false;
            console.log('Step 1 panel activated');
        }

        // Подсвечиваем рекомендуемые приложения для устройства
        highlightRecommendedApps('connection-guide-modal');
        console.log('Recommended apps highlighted');
        
        // Показываем оверлей - принудительно устанавливаем стили
        overlay.style.position = 'fixed';
        overlay.style.top = '0';
        overlay.style.left = '0';
        overlay.style.right = '0';
        overlay.style.bottom = '0';
        overlay.style.width = '100vw';
        overlay.style.height = '100vh';
        overlay.style.zIndex = '99999';
        overlay.style.display = 'block'; // Блок для позиционирования absolute внутри
        overlay.style.opacity = '0';
        overlay.style.visibility = 'hidden';
        overlay.classList.add('active');
        
        // Принудительно применяем стили после небольшой задержки
        const timer1 = setTimeout(() => {
            if (!tutorialCancelled) {
                overlay.style.opacity = '1';
                overlay.style.visibility = 'visible';
                console.log('Overlay activated, classes:', overlay.className);
                console.log('Overlay computed style:', window.getComputedStyle(overlay).display);
                console.log('Overlay opacity:', window.getComputedStyle(overlay).opacity);
            }
        }, 50);
        tutorialTimers.push(timer1);
        
        // Шаг 1 - подсвечиваем блок с приложениями (3 секунды)
        step1Target.classList.add('tutorial-highlight');
        console.log('Step 1 target highlighted');
        
        // Блокируем клики по ссылкам внутри подсвеченного блока
        const links = step1Target.querySelectorAll('a');
        links.forEach(link => {
            link.style.pointerEvents = 'none';
            link.style.cursor = 'default';
        });
        console.log(`Blocked ${links.length} links`);
        
        typeText(textEl, "Сначала вам нужно скачать Клиент на ваше устройство 📱", 50).then(() => {
            if (tutorialCancelled) return;
            console.log('✅ Step 1 text completed');
            
            // Автоматически переходим на шаг 2 через 3 секунды (всего 3 сек на шаг 1)
            const timer2 = setTimeout(() => {
                if (tutorialCancelled) return;
                step1Target.classList.remove('tutorial-highlight');
                links.forEach(link => {
                    link.style.pointerEvents = '';
                    link.style.cursor = '';
                });
                
                // Переключаем панель на шаг 2
                if (modalNextBtn) {
                    modalNextBtn.click();
                }
                
                const timer3 = setTimeout(() => {
                    if (tutorialCancelled) return;
                    // Шаг 2 - подсвечиваем блок с подпиской (3 секунды)
                    step2Target.classList.add('tutorial-highlight');
                    
                    // Блокируем клики по ссылкам внутри подсвеченного блока
                    const links2 = step2Target.querySelectorAll('a');
                    links2.forEach(link => {
                        link.style.pointerEvents = 'none';
                        link.style.cursor = 'default';
                    });
                    
                    typeText(textEl, "Затем вам нужно будет добавить подписку, чтобы всё работало 🚀", 50).then(() => {
                        if (tutorialCancelled) return;
                        console.log('✅ Step 2 text completed');
                        
                        // Автоматически закрываем туториал через 3 секунды (всего 3 сек на шаг 2)
                        const timer4 = setTimeout(() => {
                            if (tutorialCancelled) return;
                            // Завершение
                            step2Target.classList.remove('tutorial-highlight');
                            links2.forEach(link => {
                                link.style.pointerEvents = '';
                                link.style.cursor = '';
                            });
                            
                            // Скрываем оверлей
                            overlay.style.opacity = '0';
                            overlay.style.visibility = 'hidden';
                            overlay.classList.remove('active');
                            
                            if (modal) {
                                modal.style.pointerEvents = '';
                            }
                            localStorage.setItem('tutorial_shown', 'true');
                            console.log('✅ Tutorial completed');
                            
                            // Возвращаемся на 1 шаг после небольшой задержки
                            const timer5 = setTimeout(() => {
                                if (tutorialCancelled) return;
                                if (modalPrevBtn && !modalPrevBtn.disabled) {
                                    modalPrevBtn.click();
                                } else if (step1Panel && step2Panel) {
                                    // Принудительно переключаем на шаг 1
                                    step1Panel.classList.add('active');
                                    step2Panel.classList.remove('active');
                                    if (modalPrevBtn) modalPrevBtn.disabled = true;
                                    if (modalNextBtn) modalNextBtn.disabled = false;
                                    console.log('Returned to step 1');
                                }
                            }, 300);
                            tutorialTimers.push(timer5);
                        }, 3000); // 3 секунды на шаг 2
                        tutorialTimers.push(timer4);
                    });
                }, 300); // Небольшая задержка для переключения панели
                tutorialTimers.push(timer3);
            }, 3000); // 3 секунды на шаг 1 (общая длительность 6 секунд)
            tutorialTimers.push(timer2);
        });
    }

    function closeTutorial() {
        console.log('=== CLOSE TUTORIAL ===');
        
        // Устанавливаем флаг отмены туториала
        tutorialCancelled = true;
        
        // Очищаем все таймеры
        clearAllTutorialTimers();
        
        const overlay = document.getElementById('tutorial-overlay');
        const step1Target = document.getElementById('tutorial-step-1-target');
        const step2Target = document.getElementById('tutorial-step-2-target');
        const modal = document.getElementById('connection-guide-modal');
        const step1Panel = document.getElementById('step-1');
        const step2Panel = document.getElementById('step-2');
        const modalPrevBtn = document.getElementById('prev-step-btn');
        const modalNextBtn = document.getElementById('next-step-btn');
        const textEl = document.getElementById('tutorial-text');
        
        // Останавливаем печать текста
        if (textEl) {
            textEl.classList.add('completed');
            textEl.textContent = '';
        }
        
        // Убираем подсветку немедленно
        if (step1Target) {
            step1Target.classList.remove('tutorial-highlight');
            const links1 = step1Target.querySelectorAll('a');
            links1.forEach(link => {
                link.style.pointerEvents = '';
                link.style.cursor = '';
            });
        }
        if (step2Target) {
            step2Target.classList.remove('tutorial-highlight');
            const links2 = step2Target.querySelectorAll('a');
            links2.forEach(link => {
                link.style.pointerEvents = '';
                link.style.cursor = '';
            });
        }
        
        // Убираем подсветку рекомендуемых приложений
        const modalElement = document.getElementById('connection-guide-modal');
        if (modalElement) {
            modalElement.querySelectorAll('.device-badge').forEach(el => el.remove());
            modalElement.querySelectorAll('.list-item').forEach(el => el.classList.remove('recommended'));
        }
        
        // Скрываем оверлей с анимацией
        if (overlay) {
            overlay.style.opacity = '0';
            overlay.style.visibility = 'hidden';
            overlay.classList.remove('active');
            // Сбрасываем все стили оверлея
            overlay.style.position = '';
            overlay.style.top = '';
            overlay.style.left = '';
            overlay.style.right = '';
            overlay.style.bottom = '';
            overlay.style.width = '';
            overlay.style.height = '';
            overlay.style.zIndex = '';
            overlay.style.display = '';
        }
        
        // Разблокируем модалку и все её элементы
        if (modal) {
            modal.style.pointerEvents = '';
            // Разблокируем все ссылки внутри модалки
            const allLinks = modal.querySelectorAll('a');
            allLinks.forEach(link => {
                link.style.pointerEvents = '';
                link.style.cursor = '';
            });
        }
        
        // Возвращаемся на шаг 1 и восстанавливаем состояние кнопок
        if (step1Panel && step2Panel) {
            step1Panel.classList.add('active');
            step2Panel.classList.remove('active');
            if (modalPrevBtn) {
                modalPrevBtn.disabled = true;
            }
            if (modalNextBtn) {
                modalNextBtn.disabled = false;
                modalNextBtn.textContent = 'Далее';
            }
        }
        
        // Подсвечиваем рекомендуемые приложения для устройства (если модалка открыта)
        if (modal && modal.classList.contains('active')) {
            setTimeout(() => {
                highlightRecommendedApps('connection-guide-modal');
            }, 100);
        }
        
        // Сохраняем в localStorage, что туториал был пропущен
        localStorage.setItem('tutorial_shown', 'true');
        console.log('✅ Tutorial skipped and modal restored');
    }

    function highlightRecommendedApps(modalId) {
        const device = detectDevice();
        const modal = document.getElementById(modalId);
        if (!modal) return;

        // Сбрасываем предыдущие рекомендации
        modal.querySelectorAll('.device-badge').forEach(el => el.remove());
        modal.querySelectorAll('.list-item').forEach(el => el.classList.remove('recommended'));

        let selector = '';
        
        if (modalId === 'connection-guide-modal') {
            // Логика для мобильных устройств
            if (device === 'ios') selector = 'a[href*="apps.apple.com"]';
            else if (device === 'android') selector = 'a[href*="play.google.com"]';
        } else if (modalId === 'pc-guide-modal') {
            // Логика для ПК
            if (device === 'windows') selector = 'a[href*=".exe"], a[href*="microsoft"]';
            else if (device === 'macos') selector = 'a[href*=".dmg"], a[href*="apps.apple.com"]';
        }

        if (selector) {
            const links = modal.querySelectorAll(selector);
            links.forEach(link => {
                if (!link.closest('.list-item').classList.contains('recommended')) {
                    link.classList.add('recommended');
                    const textSpan = link.querySelector('.text');
                    if (textSpan) {
                        const badge = document.createElement('span');
                        badge.className = 'device-badge';
                        badge.innerHTML = 'Ваше устройство 📱';
                        textSpan.appendChild(badge);
                    }
                }
            });
        }
    }

    function copyToClipboard(text, buttonElement) {
        if (buttonElement.classList.contains('copied')) return;

        const textSpan = buttonElement.querySelector('.text');

        const markCopied = (success) => {
            if (success) {
                if (textSpan) {
                    const originalText = textSpan.textContent;
                textSpan.textContent = '✅ Скопировано!';
                buttonElement.classList.add('copied');
                setTimeout(() => {
                    textSpan.textContent = originalText;
                    buttonElement.classList.remove('copied');
                }, 2000);
                }
                
                showToast('Ссылка успешно скопирована!', 'success');
            } else {
                showToast('Не удалось скопировать', 'error');
                alert('Не удалось скопировать. Пожалуйста, выделите и скопируйте текст вручную:\n\n' + text);
            }
        };

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text)
                .then(() => markCopied(true))
                .catch(() => markCopied(false));
        } else {
            try {
                const textArea = document.createElement('textarea');
                textArea.value = text;
                textArea.style.position = 'fixed';
                textArea.style.left = '-9999px';
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                const success = document.execCommand('copy');
                document.body.removeChild(textArea);
                markCopied(success);
            } catch (err) {
                markCopied(false);
            }
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        // Проверка окружения: Telegram Mini App или обычный браузер
        if (!window.Telegram || !window.Telegram.WebApp || !window.Telegram.WebApp.initData) {
            console.log("Окружение: Обычный браузер");
        } else {
            console.log("Окружение: Telegram Mini App");
        }

        const savedTheme = localStorage.getItem('subscription_theme') || 'theme-dark';
        document.documentElement.className = savedTheme;
        const switcher = document.getElementById('theme-switcher-grid');
        if (switcher) {
            if (savedTheme === 'theme-dark') {
                switcher.classList.add('night-theme');
            } else {
                switcher.classList.remove('night-theme');
            }
        }
        
        try {
            const tg = window.Telegram.WebApp;
            tg.ready();
            tg.expand();
            if (savedTheme === 'theme-dark') {
                tg.setHeaderColor('#000000');
                tg.setBackgroundColor('#000000');
            } else {
                tg.setHeaderColor('#F2F2F7');
                tg.setBackgroundColor('#F2F2F7');
            }
        } catch (e) { console.error("Ошибка инициализации TWA.", e); }

        // ИЗМЕНЕНО: Старый код для навигации и выпадающего списка тарифов удален

        // Инициализация переключателя темы
        const themeSwitcher = document.getElementById('theme-switcher-grid');
        if (themeSwitcher) {
            themeSwitcher.addEventListener('click', toggleTheme);
        }
        
        // Инициализация прогресс-бара трафика
        function initTrafficProgress() {
            const container = document.querySelector('.traffic-progress-container');
            const progressBar = document.getElementById('traffic-progress-bar');
            const percentageEl = document.getElementById('traffic-percentage');
            
            if (container && progressBar && percentageEl) {
                const usedBytes = parseInt(container.dataset.trafficUsed || '0', 10);
                const limitBytes = parseInt(container.dataset.trafficLimit || '0', 10);
                
                if (limitBytes > 0) {
                    const percentage = Math.min(100, Math.round((usedBytes / limitBytes) * 100));
                    progressBar.style.width = percentage + '%';
                    percentageEl.textContent = percentage + '%';
                    
                    // Меняем цвет при приближении к лимиту
                    if (percentage >= 90) {
                        progressBar.style.background = 'linear-gradient(90deg, #FF3B30 0%, #FF9500 100%)';
                    } else if (percentage >= 70) {
                        progressBar.style.background = 'linear-gradient(90deg, #FF9500 0%, #FFCC00 100%)';
                    }
                }
            }
        }
        
        // Вызываем инициализацию прогресс-бара при загрузке
        initTrafficProgress();

        const openModalButtons = document.querySelectorAll('[data-modal-target]');
        const openModal = (modal) => {
            modal?.classList.add('active');
            // Сбрасываем все стили при открытии
            if (modal) {
                modal.style.opacity = '';
                const modalSheet = modal.querySelector('.modal-sheet');
                if (modalSheet) {
                    modalSheet.style.transform = '';
                }
            }
        };
        const closeModal = (modal) => {
            modal?.classList.remove('active');
            modal?.classList.remove('dragging');
            // Сбрасываем все стили при закрытии
            if (modal) {
                modal.style.opacity = '';
                const modalSheet = modal.querySelector('.modal-sheet');
                if (modalSheet) {
                    modalSheet.style.transform = '';
                }
            }
        };
        
        openModalButtons.forEach(btn => {
            const modal = document.getElementById(btn.dataset.modalTarget);
            btn.addEventListener('click', () => {
                openModal(modal);
                
                // Запускаем туториал только для кнопки "Установка на Смартфон"
                // Проверяем data-атрибут, чтобы туториал не показывался при открытии через "Добавить подписку"
                console.log('Button clicked:', {
                    modalTarget: btn.dataset.modalTarget,
                    showTutorial: btn.dataset.showTutorial,
                    shouldStart: btn.dataset.modalTarget === 'connection-guide-modal' && btn.dataset.showTutorial === 'true'
                });
                
                if (btn.dataset.modalTarget === 'connection-guide-modal' && btn.dataset.showTutorial === 'true') {
                    // Проверяем, не был ли туториал уже пропущен
                    const tutorialShown = localStorage.getItem('tutorial_shown') === 'true';
                    if (!tutorialShown) {
                        // Запускаем туториал после задержки, чтобы модалка успела отрендериться
                        setTimeout(() => {
                            console.log('🚀 Attempting to start tutorial...');
                            startTutorial();
                        }, 500);
                    } else {
                        // Если туториал уже был пропущен, просто подсвечиваем рекомендуемые приложения
                        setTimeout(() => highlightRecommendedApps('connection-guide-modal'), 100);
                    }
                } else {
                    // Для других модалок подсвечиваем устройство как обычно
                    if (btn.dataset.modalTarget === 'connection-guide-modal' || btn.dataset.modalTarget === 'pc-guide-modal') {
                        setTimeout(() => highlightRecommendedApps(btn.dataset.modalTarget), 100);
                    }
                }
            });
            modal?.addEventListener('click', e => (e.target === modal) && closeModal(modal));
            
            // Добавляем обработчики свайпа для закрытия модального окна
            if (modal) {
                let swipeState = {
                    startY: 0,
                    currentY: 0,
                    isDragging: false,
                    threshold: 100 // Минимальное расстояние свайпа для закрытия
                };
                
                const modalSheet = modal.querySelector('.modal-sheet');
                const modalHeader = modal.querySelector('.modal-header');
                if (modalSheet) {
                    // Начало касания - проверяем, что касание в верхней части модального окна
                    modalSheet.addEventListener('touchstart', (e) => {
                        if (!modal.classList.contains('active')) return;
                        
                        // Проверяем, что касание в области header или drag-handle (первые 80px)
                        const touchY = e.touches[0].clientY;
                        const sheetRect = modalSheet.getBoundingClientRect();
                        const relativeY = touchY - sheetRect.top;
                        
                        // Разрешаем свайп только если касание в верхней части (header область)
                        if (relativeY <= 80) {
                            swipeState.startY = touchY;
                            swipeState.isDragging = false;
                        }
                    }, { passive: true });
                    
                    // Движение пальца
                    modalSheet.addEventListener('touchmove', (e) => {
                        if (!modal.classList.contains('active')) return;
                        if (!swipeState.startY) return;
                        
                        swipeState.currentY = e.touches[0].clientY;
                        const deltaY = swipeState.currentY - swipeState.startY;
                        
                        // Если тянем вниз
                        if (deltaY > 0) {
                            swipeState.isDragging = true;
                            modal.classList.add('dragging');
                            // Ограничиваем максимальное смещение
                            const maxDelta = Math.min(deltaY, 300);
                            modalSheet.style.transform = `translateY(${maxDelta}px)`;
                            
                            // Добавляем прозрачность при сильном свайпе
                            const opacity = Math.max(0.3, 1 - (maxDelta / 300));
                            modal.style.opacity = opacity;
                        }
                    }, { passive: true });
                    
                    // Отпускание пальца
                    modalSheet.addEventListener('touchend', () => {
                        if (!swipeState.isDragging) {
                            swipeState.startY = 0;
                            swipeState.currentY = 0;
                            return;
                        }
                        
                        const deltaY = swipeState.currentY - swipeState.startY;
                        
                        // Если свайпнули достаточно далеко, закрываем модальное окно
                        if (deltaY >= swipeState.threshold) {
                            closeModal(modal);
                        } else {
                            // Возвращаем на место с анимацией
                            modal.classList.remove('dragging');
                            modalSheet.style.transform = '';
                            modal.style.opacity = '';
                        }
                        
                        swipeState.startY = 0;
                        swipeState.currentY = 0;
                        swipeState.isDragging = false;
                    }, { passive: true });
                }
            }
        });

        function initializeWizard(modalId, nextBtnId, prevBtnId, openTriggerSelector) {
            const modal = document.getElementById(modalId);
            if (!modal) return;
            const stepPanels = modal.querySelectorAll('.step-panel');
            const nextBtn = document.getElementById(nextBtnId);
            const prevBtn = document.getElementById(prevBtnId);
            let currentStep = 1;

            const goToStep = (step) => {
                currentStep = step;
                stepPanels.forEach((p, index) => p.classList.toggle('active', (index + 1) === currentStep));
                prevBtn.disabled = currentStep === 1;
                nextBtn.textContent = (currentStep === stepPanels.length) ? 'Готово' : 'Далее';
            };

            nextBtn.addEventListener('click', () => (currentStep < stepPanels.length) ? goToStep(currentStep + 1) : closeModal(modal));
            prevBtn.addEventListener('click', () => (currentStep > 1) ? goToStep(currentStep - 1) : null);
            
            const openTrigger = document.querySelector(openTriggerSelector);
            if (openTrigger) openTrigger.addEventListener('click', () => goToStep(1));
            
            return goToStep;
        }

        const goToConnectionStep = initializeWizard('connection-guide-modal', 'next-step-btn', 'prev-step-btn', '[data-modal-target="connection-guide-modal"]');
        initializeWizard('pc-guide-modal', 'pc-next-step-btn', 'pc-prev-step-btn', '[data-modal-target="pc-guide-modal"]');
        
        const addSubBtn = document.getElementById('add-subscription-btn');
        const connectionModal = document.getElementById('connection-guide-modal');
        if (addSubBtn && connectionModal && typeof goToConnectionStep === 'function') {
            addSubBtn.addEventListener('click', () => {
                // Открываем модалку без туториала (сразу на шаге 2)
                goToConnectionStep(2);
                connectionModal.classList.add('active');
                // Убеждаемся, что туториал не показывается
                const tutorialOverlay = document.getElementById('tutorial-overlay');
                if (tutorialOverlay) {
                    tutorialOverlay.classList.remove('active');
                }
            });
        }
        
        const shareBtn = document.getElementById('share-subscription-btn');
        if (shareBtn) {
            shareBtn.addEventListener('click', () => {
                // Используем чистый URL без параметров Telegram Web App
                const universalLink = window.location.origin + window.location.pathname;
                const shareText = `Привет! 👋\n\nДелюсь с тобой своей подпиской на быстрый и стабильный VPN. \n\nЧтобы подключиться, просто следуй инструкциям по этой ссылке:`;
                const url = `https://t.me/share/url?url=${encodeURIComponent(universalLink)}&text=${encodeURIComponent(shareText)}`;
                try {
                    window.Telegram.WebApp.openTelegramLink(url);
                } catch (e) {
                    window.open(url, '_blank');
                }
            });
        }

        const tvSendBtn = document.getElementById('tv-send-btn');
        const tvUidInput = document.getElementById('tv-uid-input');
        const tvSendStatus = document.getElementById('tv-send-status');
        if (tvSendBtn && tvUidInput && tvSendStatus) {
            tvSendBtn.addEventListener('click', async () => {
                const uid = (tvUidInput.value || '').trim();
                if (!/^[A-Za-z0-9]{5}$/.test(uid)) {
                    tvSendStatus.textContent = 'Введите корректный 5-значный UID.';
                    tvSendStatus.style.color = '#D92D20';
                    return;
                }
                tvSendStatus.textContent = 'Отправляем...';
                tvSendStatus.style.color = '';
                try {
                    const resp = await fetch(`/sendtv/${encodeURIComponent(uid)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: window.SUBSCRIPTION_BASE64 || '' })
                    });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const json = await resp.json();
                    if (json.ok) {
                        tvSendStatus.textContent = 'Готово! Подписка отправлена на TV.';
                        tvSendStatus.style.color = '#34C759';
                    } else {
                        throw new Error('Server returned not ok');
                    }
                } catch (e) {
                    tvSendStatus.textContent = 'Ошибка отправки. Попробуйте позже.';
                    tvSendStatus.style.color = '#D92D20';
                }
            });
        }

        const toggleQrBtn = document.getElementById('toggle-qr-btn');
        const manualQrBlock = document.getElementById('manual-qr-block');
        if (toggleQrBtn && manualQrBlock) {
            toggleQrBtn.addEventListener('click', () => {
                const isVisible = manualQrBlock.classList.toggle('active');
                toggleQrBtn.textContent = isVisible ? 'Скрыть QR-код' : 'Показать QR-код';
            });
        }
        
        // Обработчик кнопки "Пропустить обучение"
        const skipTutorialBtn = document.getElementById('skip-tutorial-btn');
        if (skipTutorialBtn) {
            skipTutorialBtn.addEventListener('click', () => {
                closeTutorial();
                // Тактильная обратная связь
                try {
                    window.Telegram?.WebApp?.HapticFeedback?.impactOccurred('light');
                } catch (e) {}
            });
        }

        const guideModal = document.getElementById('connection-guide-modal');
        if (guideModal) {
            const appToggleButtons = guideModal.querySelectorAll('.segmented-control button');
            const appContentPanels = guideModal.querySelectorAll('.app-content-panel');
            appToggleButtons.forEach(button => {
                button.addEventListener('click', () => {
                    appToggleButtons.forEach(btn => btn.classList.remove('active'));
                    button.classList.add('active');
                    appContentPanels.forEach(panel => panel.classList.toggle('active', panel.dataset.appContent === button.dataset.app));
                });
            });
        }
        
        // Ищем ссылки скачивания только в модалках с инструкциями
        const connectionGuideModal = document.getElementById('connection-guide-modal');
        const pcGuideModal = document.getElementById('pc-guide-modal');
        const allDownloadLinks = [];
        if (connectionGuideModal) {
            allDownloadLinks.push(...connectionGuideModal.querySelectorAll('.modal-body .list-group a[target="_blank"]'));
        }
        if (pcGuideModal) {
            allDownloadLinks.push(...pcGuideModal.querySelectorAll('.modal-body .list-group a[target="_blank"]'));
        }
        
        const isIOS = /iPad|iPhone|iPod/i.test(navigator.userAgent) && !window.MSStream;

        const warningModal = document.getElementById('download-warning-modal');
        if (warningModal) {
            const countdownTimerEl = document.getElementById('countdown-timer');
            const countdownProgressBar = warningModal.querySelector('.countdown-progress');
            let countdownInterval;

            const showWarningModal = (url, advanceToStep2) => {
                // Открываем ссылку СРАЗУ в контексте жеста пользователя.
                // setTimeout заблокирован iOS Safari и ненадёжен на Android.
                try { window.open(url, '_blank'); } catch (e) { window.location.href = url; }

                // Переходим на шаг 2 после открытия ссылки
                if (advanceToStep2 && typeof goToConnectionStep === 'function') {
                    goToConnectionStep(2);
                }

                // Модал — только информационный (навигации внутри больше нет)
                clearInterval(countdownInterval);
                countdownProgressBar.style.transition = 'none';
                countdownProgressBar.style.width = '100%';
                void countdownProgressBar.offsetWidth;
                warningModal.classList.add('active');

                countdownProgressBar.style.transition = 'width 4s linear';
                countdownProgressBar.style.width = '0%';

                let timeLeft = 4;
                countdownTimerEl.textContent = timeLeft;
                countdownInterval = setInterval(() => {
                    timeLeft--;
                    countdownTimerEl.textContent = Math.max(0, timeLeft);
                    if (timeLeft <= 0) {
                        clearInterval(countdownInterval);
                        warningModal.classList.remove('active');
                    }
                }, 1000);
            };

            allDownloadLinks.forEach(link => {
                if (link.classList.contains('auto-setup-link') || link.querySelector('.auto-setup-icon')) {
                    return;
                }
                if (link.querySelector('.icon img')) {
                    link.addEventListener('click', (e) => {
                        e.preventDefault();
                        const destinationUrl = link.href;
                        const isPhoneGuide = !!link.closest('#connection-guide-modal');
                        showWarningModal(destinationUrl, isPhoneGuide);
                    });
                }
            });
        }
    });