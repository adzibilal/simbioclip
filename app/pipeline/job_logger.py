import json
import logging
import os


class JobFileHandler(logging.Handler):
    """Writes structured JSON log entries to {job_dir}/pipeline.log (one per line)."""

    def __init__(self, log_path: str):
        super().__init__()
        self.log_path = log_path
        # Truncate the file so each new pipeline run starts fresh
        open(log_path, "w", encoding="utf-8").close()

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name.replace("simbioclip.", ""),
                "msg": record.getMessage(),
            }
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)


def attach_job_logger(job_dir: str) -> JobFileHandler:
    log_path = os.path.join(job_dir, "pipeline.log")
    handler = JobFileHandler(log_path)
    handler.setLevel(logging.DEBUG)
    logging.getLogger("simbioclip").addHandler(handler)
    return handler


def detach_job_logger(handler: JobFileHandler) -> None:
    logging.getLogger("simbioclip").removeHandler(handler)
