"""
reset_db_full.py — Alias de seed_all.py para compatibilidad.
Ejecuta el reset completo: BD + wizard + PDF + chunks pgvector + tickets.
"""
from seed_all import run

if __name__ == "__main__":
    run()
