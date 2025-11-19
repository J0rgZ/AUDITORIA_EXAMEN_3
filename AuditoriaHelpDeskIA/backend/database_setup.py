# backend/database_setup.py
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "data/tickets.db")

def setup_database():
    # Asegurarse de que el directorio existe
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    # Crear la conexión (SQLite creará el archivo si no existe)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ÚNICAMENTE creamos la tabla de tickets
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        status TEXT NOT NULL
    )
    ''')

    conn.commit()
    conn.close()
    print("Base de datos 'tickets.db' y tabla 'tickets' configuradas correctamente.")

if __name__ == "__main__":
    setup_database()