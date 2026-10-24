from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json

app = FastAPI()

# --- CONFIGURACIÓN CORS (Permite enviar datos, leer y BORRAR) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  # <--- Asterisco clave para habilitar DELETE
    allow_headers=["*"],
)

# --- BASE DE DATOS ---
def iniciar_db():
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS componentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mpn TEXT UNIQUE NOT NULL,
            nombre TEXT NOT NULL,
            stock INTEGER NOT NULL,
            datasheet_url TEXT,
            especificaciones TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS proyectos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lista_materiales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proyecto_id INTEGER,
            componente_id INTEGER,
            cantidad_necesaria INTEGER NOT NULL,
            FOREIGN KEY(proyecto_id) REFERENCES proyectos(id),
            FOREIGN KEY(componente_id) REFERENCES componentes(id)
        )
    """)
    conexion.commit()
    conexion.close()

iniciar_db()

# --- MODELOS ---
class ComponenteWeb(BaseModel):
    mpn: str
    nombre: str
    stock: int

class Proyecto(BaseModel):
    nombre: str

class ComponenteProyecto(BaseModel):
    proyecto_id: int
    componente_id: int
    cantidad: int

# --- RUTAS DE INVENTARIO ---

@app.get("/componentes")
def obtener_componentes():
    conexion = sqlite3.connect("electrostock.db")
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM componentes")
    componentes = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return componentes

@app.post("/agregar_componente_web")
def agregar_componente_web(comp: ComponenteWeb):
    # Enlace dinámico de datasheet y especificaciones base
    datasheet = f"https://octopart.com/search?q={comp.mpn}"
    specs = json.dumps({"Encapsulado": "ND", "RoHS": "Sí", "Categoría": "General"})
    
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "INSERT INTO componentes (mpn, nombre, stock, datasheet_url, especificaciones) VALUES (?, ?, ?, ?, ?)",
            (comp.mpn, comp.nombre, comp.stock, datasheet, specs)
        )
        conexion.commit()
    except sqlite3.IntegrityError:
        conexion.close()
        raise HTTPException(status_code=400, detail="El componente ya existe en el inventario")
    conexion.close()
    return {"mensaje": f"Componente {comp.mpn} agregado con éxito"}

@app.delete("/componentes/{componente_id}")
def borrar_componente(componente_id: int):
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    
    # 1. Verificar si el componente existe
    cursor.execute("SELECT id FROM componentes WHERE id = ?", (componente_id,))
    if not cursor.fetchone():
        conexion.close()
        raise HTTPException(status_code=404, detail="Componente no encontrado")
        
    # 2. Borrarlo del inventario principal
    cursor.execute("DELETE FROM componentes WHERE id = ?", (componente_id,))
    
    # 3. Borrarlo también de las listas de materiales (BOM) para evitar datos fantasmas
    cursor.execute("DELETE FROM lista_materiales WHERE componente_id = ?", (componente_id,))
    
    conexion.commit()
    conexion.close()
    return {"mensaje": "Componente eliminado con éxito"}

# --- RUTAS DE PROYECTOS ---

@app.get("/proyectos")
def obtener_proyectos():
    conexion = sqlite3.connect("electrostock.db")
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM proyectos")
    proyectos = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return proyectos

@app.post("/proyectos")
def crear_proyecto(proy: Proyecto):
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    try:
        cursor.execute("INSERT INTO proyectos (nombre) VALUES (?)", (proy.nombre,))
        conexion.commit()
    except sqlite3.IntegrityError:
        conexion.close()
        raise HTTPException(status_code=400, detail="El proyecto ya existe")
    conexion.close()
    return {"mensaje": "Proyecto creado con éxito"}

@app.get("/lista_materiales/{proyecto_id}")
def obtener_bom(proyecto_id: int):
    conexion = sqlite3.connect("electrostock.db")
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute('''
        SELECT lm.id, c.mpn, c.nombre, lm.cantidad_necesaria 
        FROM lista_materiales lm
        JOIN componentes c ON lm.componente_id = c.id
        WHERE lm.proyecto_id = ?
    ''', (proyecto_id,))
    bom = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return bom

@app.post("/lista_materiales")
def agregar_a_bom(item: ComponenteProyecto):
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    cursor.execute(
        "INSERT INTO lista_materiales (proyecto_id, componente_id, cantidad_necesaria) VALUES (?, ?, ?)",
        (item.proyecto_id, item.componente_id, item.cantidad)
    )
    conexion.commit()
    conexion.close()
    return {"mensaje": "Componente añadido a la lista de materiales"}

@app.post("/ensamblar_proyecto/{proyecto_id}")
def ensamblar_proyecto(proyecto_id: int):
    conexion = sqlite3.connect("electrostock.db")
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    
    cursor.execute("SELECT componente_id, cantidad_necesaria FROM lista_materiales WHERE proyecto_id = ?", (proyecto_id,))
    materiales = cursor.fetchall()
    
    if not materiales:
        conexion.close()
        raise HTTPException(status_code=400, detail="El proyecto no tiene componentes asignados.")
        
    for mat in materiales:
        comp_id = mat["componente_id"]
        cant_req = mat["cantidad_necesaria"]
        
        cursor.execute("SELECT stock, mpn FROM componentes WHERE id = ?", (comp_id,))
        comp_db = cursor.fetchone()
        
        if not comp_db:
            conexion.close()
            raise HTTPException(status_code=400, detail="Componente no encontrado en inventario")
            
        if comp_db["stock"] < cant_req:
            conexion.close()
            raise HTTPException(status_code=400, detail=f"Stock insuficiente para MPN: {comp_db['mpn']}. Se necesitan {cant_req}, pero hay {comp_db['stock']}.")
            
    for mat in materiales:
        cursor.execute("UPDATE componentes SET stock = stock - ? WHERE id = ?", (mat["cantidad_necesaria"], mat["componente_id"]))
        
    conexion.commit()
    conexion.close()
    return {"mensaje": "Ensamblaje exitoso. Se descontó el stock del inventario."}