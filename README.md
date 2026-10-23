# Media Downloader API

FastAPI-сервис для LLM-агента: скачивает видео/аудио (YouTube, TikTok и всё, что поддерживает `yt-dlp`), при необходимости обрезает через `ffmpeg` и возвращает публичную прямую ссылку на файл.

## Переменные окружения

| Переменная        | Обязательна | Описание                                                                    |
|-------------------|-------------|-----------------------------------------------------------------------------|
| `API_KEY`         | да          | Ключ для заголовка `X-API-Key`                                              |
| `PUBLIC_BASE_URL` | желательно  | Публичный адрес, например `https://media.example.com`. Если пусто — берётся из запроса |
| `FILE_TTL_HOURS`  | нет (24)    | Через сколько часов удалять файлы (`0` — не удалять)                        |
| `MAX_FILESIZE_MB` | нет (1024)  | Лимит размера одного скачивания (`0` — без лимита)                          |
| `SUPABASE_URL`    | нет         | Project URL из Supabase. Если задан вместе с ключом, файлы загружаются в Supabase Storage |
| `SUPABASE_SERVICE_KEY` | нет    | `service_role` / secret key (Project Settings → API). Только на сервере, никому не отдавать |
| `SUPABASE_BUCKET` | нет (`media`) | Публичный bucket. Создаётся автоматически при старте                      |
| `SUPABASE_MAX_MB` | нет (50)    | Максимальный размер загружаемого файла (лимит бесплатного плана — 50 МБ)    |
| `COOKIES_FILE`    | нет         | Путь к `cookies.txt` для `yt-dlp`, если YouTube блокирует IP сервера        |
| `COOKIES_B64`     | нет         | Содержимое `cookies.txt` в base64 (альтернатива `COOKIES_FILE`, удобно на Render) |
| `APIFY_TOKEN`     | нет         | Токен Apify. Если задан, YouTube скачивается через `streamers/youtube-video-downloader` |
| `YOUTUBE_VIA_APIFY` | нет (`fallback`) | `always`: YouTube только через Apify. `fallback`: сначала `yt-dlp`, Apify при ошибке |
| `APIFY_VIDEO_QUALITY` / `APIFY_AUDIO_QUALITY` | нет (`720p` / `360p`) | Качество, которое запрашивается у Apify. Для аудио берётся дешёвое видео, из которого извлекается mp3 |

### YouTube на облачных серверах

YouTube блокирует IP большинства облачных серверов (Render в том числе): ответы 403/429, и cookies это не обходят. Поэтому на Render YouTube скачивается через Apify (`YOUTUBE_VIA_APIFY=always`): примерно $0.02 за короткий ролик в 720p, на бесплатном аккаунте Apify $5 кредитов в месяц. Обрезка, mp3 и загрузка в Supabase выполняются на нашем сервере. Ограничения: один запуск Apify — максимум 5 минут, видео приходит в VP9 (не H.264).

## Деплой: Render (free) + Supabase Storage

На бесплатном Render диск временный, поэтому готовые файлы загружаются в Supabase, и ссылки не пропадают, когда сервис засыпает.

1. **Supabase:** создать проект на supabase.com → Project Settings → API → скопировать *Project URL* и *service_role / secret key*. Bucket `media` сервис создаст сам при первом старте.
2. **GitHub:** залить этот репозиторий.
3. **Render:** New → Blueprint → выбрать репозиторий (используется `render.yaml`). Render спросит `SUPABASE_URL` и `SUPABASE_SERVICE_KEY`: вставить их. `API_KEY` будет сгенерирован автоматически (смотреть в Environment).
4. Проверить `https://<сервис>.onrender.com/health`. Должно вернуться `{"status":"ok","storage":"supabase"}`.

Особенности:
- Бесплатный Render засыпает через 15 минут без запросов. Первый запрос после паузы ждёт около минуты (разбудить можно, открыв `/health`).
- Если результат больше `SUPABASE_MAX_MB`, сервис вернёт `413` с подсказкой сократить отрезок или взять `audio`.
- Файлы в bucket старше `FILE_TTL_HOURS` удаляются при каждом запросе.
- Если YouTube отвечает «Sign in to confirm you're not a bot»: экспортировать cookies из браузера (расширение «Get cookies.txt LOCALLY») и добавить в Render как Secret File с именем `cookies.txt`.

## Запуск на сервере (Docker)

```bash
# 1. Скопировать проект на VPS и перейти в папку
cd media-api

# 2. Создать .env
cp .env.example .env
nano .env            # задать API_KEY и PUBLIC_BASE_URL
# сгенерировать ключ: openssl rand -hex 32

# 3. Собрать образ
docker build -t media-api .

# 4. Запустить
docker run -d --name media-api \
  --restart unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  -v $(pwd)/public_downloads:/app/public_downloads \
  media-api

# Логи
docker logs -f media-api
```

Проверка: `curl http://<IP>:8000/health` → `{"status":"ok"}`.

Для HTTPS поставьте перед контейнером reverse proxy (Caddy / nginx / Traefik). Пример для Caddy (`/etc/caddy/Caddyfile`):

```
media.example.com {
    reverse_proxy localhost:8000
}
```

### Обновление yt-dlp

Сайты часто меняются, поэтому при ошибках скачивания просто пересоберите образ без кэша:

```bash
docker build --no-cache -t media-api . && docker rm -f media-api && docker run ... (как выше)
```

## API

`POST /api/process_media`, заголовок `X-API-Key: <ключ>`

```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "media_type": "video",
  "start_time": "00:00:10",
  "end_time": "00:00:25"
}
```

- `media_type`: `video` (mp4) или `audio` (mp3), по умолчанию `video`
- `start_time` / `end_time`: необязательны, формат `HH:MM:SS`, `MM:SS` или секунды (`42.5`)
- `precise`: `false` по умолчанию. При `true` фрагмент перекодируется (H.264/AAC или mp3), чтобы обрезка была точной до кадра. Это медленнее, но длительность совпадает с запрошенной

Ответ:

```json
{
  "status": "success",
  "file_url": "https://media.example.com/downloads/3f2a9c...e1.mp4"
}
```

Коды ошибок: `401` — неверный ключ, `422` — неверные параметры/таймкоды, `400` — `yt-dlp` не смог скачать, `413` — файл больше лимита Supabase, `502` — не удалось загрузить в Supabase, `500` — внутренняя ошибка.

### Управление хранилищем

Работает и с Supabase, и с локальной папкой. Везде нужен заголовок `X-API-Key`.

| Эндпоинт | Что делает |
|---|---|
| `GET /api/files` | Список файлов (новые первыми): имя, размер, дата, ссылка, плюс общий объём |
| `DELETE /api/files/{name}` | Удалить один файл по имени (например, `d736cf....mp4`) |
| `POST /api/cleanup` `{"older_than_hours": 24}` | Удалить файлы старше N часов. `0` удаляет всё |

Кроме того, при каждом запросе к `/api/process_media` автоматически удаляются файлы старше `FILE_TTL_HOURS`.

```bash
curl -H "X-API-Key: YOUR_API_KEY" https://media.example.com/api/files
curl -X POST https://media.example.com/api/cleanup -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" -d '{"older_than_hours": 0}'
```

## Примеры cURL

Видео с обрезкой:

```bash
curl -X POST "https://media.example.com/api/process_media" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "media_type": "video", "start_time": "00:00:10", "end_time": "00:00:25"}'
```

Точная обрезка (с перекодированием):

```bash
curl -X POST "https://media.example.com/api/process_media" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "start_time": "00:00:10", "end_time": "00:00:25", "precise": true}'
```

Только аудио (mp3) целиком:

```bash
curl -X POST "https://media.example.com/api/process_media" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"url": "https://www.tiktok.com/@user/video/1234567890", "media_type": "audio"}'
```

## Замечания

- По умолчанию обрезка идёт с `-c copy` (без перекодирования). Это быстро, но начало видеофрагмента «прилипает» к ближайшему ключевому кадру, поэтому может сдвинуться на несколько секунд. Если важна точная длительность, передайте `"precise": true`.
- Эндпоинт синхронный: запрос держится открытым до конца скачивания. Для длинных видео увеличьте таймауты у reverse proxy и у HTTP-клиента агента.
- Swagger-документация доступна на `/docs` — удобно, чтобы отдать агенту OpenAPI-схему как описание инструмента.
