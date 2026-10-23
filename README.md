# Media Downloader API

FastAPI-сервис для LLM-агента: скачивает видео/аудио (YouTube, TikTok и всё, что поддерживает `yt-dlp`), при необходимости обрезает через `ffmpeg` и возвращает публичную прямую ссылку на файл.

## Переменные окружения

| Переменная        | Обязательна | Описание                                                                    |
|-------------------|-------------|-----------------------------------------------------------------------------|
| `API_KEY`         | да          | Ключ для заголовка `X-API-Key`                                              |
| `PUBLIC_BASE_URL` | желательно  | Публичный адрес, например `https://media.example.com`. Если пусто — берётся из запроса |
| `FILE_TTL_HOURS`  | нет (24)    | Через сколько часов удалять файлы (`0` — не удалять)                        |
| `MAX_FILESIZE_MB` | нет (1024)  | Лимит размера одного скачивания (`0` — без лимита)                          |

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

Коды ошибок: `401` — неверный ключ, `422` — неверные параметры/таймкоды, `400` — `yt-dlp` не смог скачать, `500` — внутренняя ошибка.

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
