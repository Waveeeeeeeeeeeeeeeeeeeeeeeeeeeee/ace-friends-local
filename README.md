# ace-friends-local

Локальный бэкенд ACE Friends с готовой тестовой базой и фотографиями.

## Запуск

Нужны Docker с Compose 2.20+ и репозитории сервисов из ветки `development`
рядом с папкой `ace-friends-local`: `ace-friends-database`, `ace-friends-auth`,
`ace-friends-dating`, `ace-friends-admin`, `ace-friends-gis`, `ace-friends-ai`,
`ace-friends-notifies`, `ace-friends-scheduler`, `ace-friends-tg-bot`.

Из папки `ace-friends-local`:

```sh
docker compose up -d
```

`bootstrap` подготовит Vault, пароли и настройки сервисов.
`seed` загрузит базу и фотографии, заполнит поиск Qdrant.
После подготовки они завершаются с `Exited (0)` — это нормально.
Повторный запуск сохраняет существующие данные и пароли.

В тестовой базе 2005 пользователей, 3204 анкеты и 31 игра.
**В GIS по умолчанию загружены только Россия и Москва.**

## Подключение

| Сервис | Адрес |
|---|---|
| Swagger Auth | http://localhost:15100/docs |
| Swagger Database | http://localhost:15000/docs |
| Swagger Dating | http://localhost:15300/docs |
| Swagger Admin / GIS | http://localhost:15600/docs / http://localhost:15200/docs |
| PostgreSQL | `localhost:15432`, база `ace_local`, пользователь `ace` |
| Vault | http://localhost:18200 |
| Qdrant | http://localhost:16333/dashboard |
| S3 | http://localhost:19000 |
| Письма Mailpit | http://localhost:18025 |

Администратор: `admin@ace.local`. Пароли лежат в `.private/credentials.json`:
`admin_password` — администратора, `postgres_password` — PostgreSQL.
Тестовый пользователь: `language-scenario-1@example.com`, вход по коду из Mailpit.

## Основные команды

```sh
docker compose ps -a                         # состояние контейнеров
docker compose logs --tail 50 dating         # логи Dating
docker compose restart dating               # применить правки в server/
docker compose up -d --build --no-deps dating # обновить образ после изменения зависимостей
docker compose exec postgres psql -U ace -d ace_local # открыть БД
docker compose stop                         # остановить стенд
```

База и фотографии сохраняются в Docker-томах. Локальные ключи и настройки —
в `.private/`; эту папку не передают другим разработчикам.
Telegram-бот выключен, для AI нужны собственные тестовые ключи.
