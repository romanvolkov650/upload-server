import json
import os
import secrets
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path as PathParam, Response, UploadFile
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse

DATA_DIR = Path(__file__).parent / "data"
INCOMING = DATA_DIR / "incoming"      # готовые, ещё не забранные батчи
PROCESSING = DATA_DIR / "processing"  # батчи, забранные текущими /download
for d in (INCOMING, PROCESSING):
    d.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ на файл
MAX_META_SIZE = 64 * 1024         # 64 КБ на метаданные
META_NAME = "meta.json"
MAX_DISK_USAGE = float(os.environ.get("MAX_DISK_USAGE", 95))  # % занятости диска
CLAIM_TTL = 10 * 60  # сек: сколько ждём подтверждения, потом батчи возвращаются в очередь
CLAIM_ID = PathParam(pattern=r"^[0-9a-f]{32}$")

API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    raise RuntimeError("Переменная окружения API_KEY не задана")

_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Depends(_key_header)) -> None:
    if not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(401, "Неверный или отсутствующий X-API-Key")


# ключ проверяется на всех эндпоинтах; docs/redoc/openapi.json отключены
app = FastAPI(
    title="Image batch server",
    dependencies=[Depends(require_api_key)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def requeue(claim: Path) -> None:
    """Возвращает батчи неподтверждённой выдачи в очередь и удаляет её."""
    batches = claim / "batches"
    if batches.is_dir():
        for batch in batches.iterdir():
            batch.rename(INCOMING / batch.name)
    shutil.rmtree(claim, ignore_errors=True)


def requeue_stale(ttl: float = CLAIM_TTL) -> None:
    for claim in PROCESSING.iterdir():
        try:
            ready = (claim / "archive.zip").exists()
            if not ready or time.time() - claim.stat().st_mtime > ttl:
                requeue(claim)
        except FileNotFoundError:
            pass  # параллельно подтвердили/удалили


@app.on_event("startup")
def recover() -> None:
    for tmp in DATA_DIR.glob(".tmp_*"):
        shutil.rmtree(tmp, ignore_errors=True)
    # выдачи без готового архива (упали при сборке) вернутся сразу,
    # готовые дождутся подтверждения или истечения TTL
    requeue_stale()


def disk_usage_percent() -> float:
    u = shutil.disk_usage(DATA_DIR)
    return u.used / (u.used + u.free) * 100  # как «Use%» в df


def ensure_disk_space() -> None:
    if disk_usage_percent() >= MAX_DISK_USAGE:
        raise HTTPException(507, "Диск сервера заполнен, загрузка временно недоступна")


def parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    if len(raw) > MAX_META_SIZE:
        raise HTTPException(413, "Метаданные слишком большие")
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "metadata должен быть JSON-объектом")
    if not isinstance(meta, dict):
        raise HTTPException(400, "metadata должен быть JSON-объектом")
    return meta


@app.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    metadata: str | None = Form(None, description='JSON-объект, например {"camera": "1"}'),
):
    """Один запрос = один батч: картинки + meta.json."""
    ensure_disk_space()
    meta = parse_metadata(metadata)

    batch_id = uuid.uuid4().hex
    tmp_dir = DATA_DIR / f".tmp_{batch_id}"
    tmp_dir.mkdir()
    try:
        for i, f in enumerate(files):
            ext = Path(f.filename or "").suffix.lower()
            if ext not in ALLOWED_EXT or not (f.content_type or "").startswith("image/"):
                raise HTTPException(400, f"Недопустимый файл: {f.filename}")

            ensure_disk_space()  # диск мог заполниться, пока идёт большая загрузка
            safe_name = f"{i:03d}_{Path(f.filename).name}"  # защита от path traversal
            size = 0
            with open(tmp_dir / safe_name, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise HTTPException(413, f"Файл слишком большой: {f.filename}")
                    out.write(chunk)

        (tmp_dir / META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # атомарно публикуем готовый батч: /download никогда не увидит недописанный
    tmp_dir.rename(INCOMING / batch_id)
    return {"batch_id": batch_id, "count": len(files)}


def archive_response(claim: Path) -> FileResponse:
    return FileResponse(
        claim / "archive.zip",
        media_type="application/zip",
        filename="images.zip",
        headers={"X-Claim-Id": claim.name},
    )


@app.get("/download")
def download():
    """Забирает ВСЕ накопившиеся батчи и отдаёт архив.

    Данные остаются на сервере, пока клиент не подтвердит получение
    через DELETE /download/{claim_id}. Id выдачи — в заголовке X-Claim-Id.
    """
    requeue_stale()

    claim = PROCESSING / uuid.uuid4().hex
    batches = claim / "batches"
    batches.mkdir(parents=True)

    # rename атомарен: каждый батч достанется ровно одному /download,
    # а загруженные позже останутся для следующего
    claimed = []
    for batch in INCOMING.iterdir():
        try:
            batch.rename(batches / batch.name)
            claimed.append(batches / batch.name)
        except FileNotFoundError:
            pass  # его успел забрать параллельный запрос

    if not claimed:
        shutil.rmtree(claim)
        return Response(status_code=204)

    try:
        tmp_zip = claim / "archive.zip.tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED) as zf:  # картинки уже сжаты
            for batch in claimed:
                for p in sorted(batch.iterdir()):
                    zf.write(p, arcname=f"{batch.name}/{p.name}")
        tmp_zip.rename(claim / "archive.zip")  # архив «готов» только после rename
    except Exception:
        requeue(claim)
        raise

    return archive_response(claim)


@app.get("/download/{claim_id}")
def redownload(claim_id: str = CLAIM_ID):
    """Повторно отдаёт тот же архив, если скачивание оборвалось."""
    claim = PROCESSING / claim_id
    if not (claim / "archive.zip").exists():
        raise HTTPException(404, "Выдача не найдена или истекла")
    return archive_response(claim)


@app.delete("/download/{claim_id}", status_code=204)
def confirm(claim_id: str = CLAIM_ID):
    """Подтверждение получения: удаляет архив и картинки с сервера (идемпотентно)."""
    shutil.rmtree(PROCESSING / claim_id, ignore_errors=True)
    return Response(status_code=204)
