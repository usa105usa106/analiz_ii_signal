# Crypto Futures Signal Bot v0.01 — Railway v004

Исправление v004:

- `/start` теперь реально вызывает `ensure_admin_claim(...)`;
- первый чат, который отправит `/start`, сохраняется как `admin_id`;
- если после деплоя всё ещё пишет «Сначала админ...», значит Railway запустил старую версию или не был сделан Redeploy.

## Деплой

1. Распакуй архив.
2. Залей файлы в GitHub.
3. Railway → Deploy/Redeploy.
4. В Variables добавь `TELEGRAM_BOT_TOKEN`.
5. После запуска первым сообщением отправь боту `/start`.

Ожидаемый ответ:

```text
✅ Ты назначен админом, потому что первым отправил /start.
```

Если был старый volume/state и админ уже назначен другому чату, удали файл `state.json` из Railway volume или сделай новый Railway service без старого volume.

API-ключи вводятся через чат:

```text
api mexc
api bingx
api status
api delete mexc
api delete bingx
cancel
```

Создавай API-ключи без права вывода средств.
