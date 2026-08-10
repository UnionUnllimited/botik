/**
 * Справка: docker-compose.yml при SSH-установке Remnawave Node.
 * @see web_admin/core/rw_node_ssh_installer.py build_remnanode_compose
 */
(function (R) {
  var composeExample =
    'services:\n' +
    '  remnanode:\n' +
    '    container_name: remnanode\n' +
    '    hostname: remnanode\n' +
    '    image: remnawave/node:latest\n' +
    '    network_mode: host\n' +
    '    restart: always\n' +
    '    cap_add:\n' +
    '      - NET_ADMIN\n' +
    '    ulimits:\n' +
    '      nofile:\n' +
    '        soft: 1048576\n' +
    '        hard: 1048576\n' +
    '    volumes:\n' +
    '      - /etc/letsencrypt:/etc/letsencrypt:ro\n' +
    '      - /var/log/remnanode:/var/log/remnanode\n' +
    '    environment:\n' +
    '      - NODE_PORT=2222\n' +
    '      - SECRET_KEY="…ключ из панели Remnawave…"';

  R.remnanode_compose = {
    title: 'Пример docker-compose.yml',
    body:
      '<p>При установке через SSH создаётся файл ' +
      '<code class="tw-kbd">/opt/remnanode/docker-compose.yml</code> ' +
      'с таким содержимым (порт и <code class="tw-kbd">SECRET_KEY</code> подставляются автоматически):</p>' +
      '<pre class="admin-info-compose-pre"><code>' + composeExample + '</code></pre>' +
      '<p><code class="tw-kbd">SECRET_KEY</code> берётся из панели Remnawave (API keygen). ' +
      '<code class="tw-kbd">NODE_PORT</code> — internal-порт из формы установки. ' +
      'После <code class="tw-kbd">docker compose up -d</code> добавьте Host в разделе <strong>Hosts</strong> панели.</p>',
  };
})(window.AdminInfoRegistry = window.AdminInfoRegistry || {});
