import pathlib
import sys

# Корінь репозиторію в sys.path, щоб `import app` працював без встановлення пакета
sys.path.insert(0, str(pathlib.Path(__file__).parent))
