"""Файлы контента на диске (data/content) и их публичные адреса через Mini App (нужны Threads/Instagram)."""
import os
import secrets


class MediaStore:
    def __init__(self, root: str, public_base: str | None = None):
        self.root = root
        # MINIAPP_URL вида https://host/app → https://host
        self.public_base = public_base.rstrip("/").removesuffix("/app") if public_base else None
        os.makedirs(root, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def save(self, data: bytes, ext: str) -> str:
        """Случайное имя — файлы раздаются без авторизации, адрес не должен угадываться."""
        name = f"{secrets.token_urlsafe(12)}{ext if ext.startswith('.') else '.' + ext}"
        with open(self.path(name), "wb") as f:
            f.write(data)
        return name

    def read(self, name: str) -> bytes:
        with open(self.path(name), "rb") as f:
            return f.read()

    def url(self, name: str) -> str | None:
        return f"{self.public_base}/media/{name}" if self.public_base else None

    async def public_url(self, data: bytes, ext: str) -> str:
        if not self.public_base:
            raise RuntimeError("Для публикации картинок и видео в Threads/Instagram нужен MINIAPP_URL (публичный HTTPS)")
        return self.url(self.save(data, ext))

    def reserve(self, ext: str) -> str:
        """Имя будущего файла без создания: если сборка упадёт, на диске не останется пустышки."""
        return f"{secrets.token_urlsafe(12)}{ext if ext.startswith('.') else '.' + ext}"

    def discard(self, name: str) -> None:
        try:
            os.remove(self.path(name))
        except OSError:
            pass
