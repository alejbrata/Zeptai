import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configura el sistema de logging de la aplicación.
    Usa %(name)s para que cada módulo identifique su origen.
    """
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=date_fmt,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # Reducir ruido de librerías de terceros
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
