# SmartKruzhok

Telegram-бот по ТЗ 0.2. Короткие видео превращаются в нативные кружки с автоматическим кадрированием. Видео длиннее минуты можно разделить на серию. Под результатом доступны переделка кадра, русские автосубтитры, надписи, VHS и зум на лицо.

## Запуск через Docker

1. Создайте тестового бота командой `/newbot` в [@BotFather](https://t.me/BotFather).
2. Скопируйте `.env.example` в `.env` и укажите `BOT_TOKEN`. Храните токен только в локальном файле или в переменных окружения сервера.
3. Из корня проекта запустите:

   ```sh
   docker compose up --build -d
   docker compose logs -f bot
   ```

4. Откройте своего бота в Telegram, отправьте `/start` и загрузите MP4, MOV или WebM до 20 МБ и 3 минут.

При первом запросе субтитров бот скачает модель Faster Whisper `base` в постоянный том. Модель можно загрузить заранее после запуска контейнера:

```sh
docker compose exec bot python -c 'import os; from faster_whisper import WhisperModel; WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8", download_root="/data/models")'
```

## Локальный запуск на macOS или Linux

Понадобятся Python 3.11 и FFmpeg с `ffprobe` и `libx264`. На Linux установите шрифт DejaVu Sans Bold или задайте путь к шрифту с кириллицей в `FONT_PATH`.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p models
curl -fL https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx -o models/yunet.onnx
set -a
. ./.env
set +a
FACE_MODEL="$PWD/models/yunet.onnx" .venv/bin/python -m smartkruzhok
```

В Docker модель YuNet устанавливается при сборке. При локальном запуске без `FACE_MODEL` используется детектор лиц OpenCV Haar.

Очередь и временные данные находятся в `DATA_DIR` (локально `./data`, в Docker `/data`). Запускайте один экземпляр на каталог данных. После выдачи результата исходник доступен для редактирования 30 минут, затем фоновая уборка его удаляет. В журналах сохраняются этапы обработки, время ожидания и коды ошибок.

Для проверки в Telegram загрузите короткое видео с движущимся лицом, ролик без звука и ролик на 61 секунду. У короткого видео попробуйте сменить кадр, добавить русские субтитры и надпись, посмотреть эффект и отправить оформленный кружок. Длинное видео должно вернуться двумя кружками в порядке воспроизведения.
