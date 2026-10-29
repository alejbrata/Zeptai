import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'TICKETIA_PRO'))

from app import app  # noqa: E402

if __name__ == '__main__':
    app.run()
