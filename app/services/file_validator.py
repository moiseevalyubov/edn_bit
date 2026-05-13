MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    # Windows executables
    "exe", "dll", "msi", "com", "scr", "pif",
    # Windows scripts
    "bat", "cmd", "ps1", "vbs", "wsf",
    # Unix scripts
    "sh", "bash", "zsh", "csh",
    # Web/server scripts
    "js", "ts", "jsx", "tsx", "py", "rb", "php",
    # Java
    "jar", "class", "war", "ear",
    # Web/XSS vectors
    "html", "htm", "svg",
})


def check_extension(filename: str) -> str | None:
    """Returns an error message if the file extension is blocked or absent, otherwise None."""
    if "." not in filename:
        return f"Файл «{filename}» не отправлен: недопустимый тип файла"
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in BLOCKED_EXTENSIONS:
        return f"Файл «{filename}» не отправлен: недопустимый тип файла"
    return None


def check_size(content: bytes, filename: str) -> str | None:
    """Returns an error message if the file exceeds MAX_FILE_SIZE, otherwise None."""
    if len(content) > MAX_FILE_SIZE:
        size_mb = len(content) / (1024 * 1024)
        return f"Файл «{filename}» не отправлен: размер {size_mb:.1f} МБ превышает лимит 50 МБ"
    return None
