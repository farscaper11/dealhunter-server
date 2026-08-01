def detect_storage(text: str) -> int | None:
    match = re.search(
        r"\b(1|2|4)\s*TB\s*SSD",
        text or "",
        re.I,
    )
    if match:
        return int(match.group(1)) * 1024

    match = re.search(
        r"\b(256|512|1024|2048|4096)\s*GB\s*SSD",
        text or "",
        re.I,
    )
    return int(match.group(1)) if match else None
