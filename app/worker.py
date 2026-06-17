import sys
import logging
from redis import Redis
from rq import Queue, Worker
from app.config import REDIS_URL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("simbioclip.worker")

def start_worker():
    logger.info(f"Connecting to Redis at: {REDIS_URL}")
    try:
        redis_conn = Redis.from_url(REDIS_URL)
        # Check connection
        redis_conn.ping()
        logger.info("Connected to Redis successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        sys.exit(1)

    logger.info("Starting RQ worker for queue: 'default'")
    # Pass connection directly to Queue and Worker (compatible with all RQ versions)
    queue = Queue("default", connection=redis_conn)
    worker = Worker([queue], connection=redis_conn)
    worker.work()

if __name__ == "__main__":
    start_worker()
