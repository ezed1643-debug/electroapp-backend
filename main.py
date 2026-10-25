from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json
import google.generativeai as genai
import os

app = FastAPI()

# --- CONFIGURACIÓN CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
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
    cursor.execute("CREATE TABLE IF NOT EXISTS proyectos (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT UNIQUE NOT NULL)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lista_materiales (
            id INTEGER PRIMARY KEY AUTOINCREMENT, proyecto_id INTEGER, componente_id INTEGER, cantidad_necesaria INTEGER NOT NULL,
            FOREIGN KEY(proyecto_id) REFERENCES proyectos(id), FOREIGN KEY(componente_id) REFERENCES componentes(id)
        )
    """)
    conexion.commit()
    conexion.close()

iniciar_db()

# --- EXTRACCIÓN INTELIGENTE CON IA (GOOGLE GEMINI) ---
def buscar_specs_ia(mpn: str):
    clave_secreta = os.getenv("GEMINI_API_KEY") 
    
    if not clave_secreta:
        return {"Aviso": "Error: GEMINI_API_KEY no configurada en Render."}
        
    genai.configure(api_key=clave_secreta)
    
    # Hemos modificado el prompt para pedir hasta 10 especificaciones detalladas
    prompt = f"""
    Eres un ingeniero electrónico experto. Necesito hasta 10 especificaciones técnicas clave del componente electrónico '{mpn}'.
    Devuelve ÚNICAMENTE un objeto JSON válido. No uses markdown, no uses bloques de código (```json), no agregues texto adicional.
    Ejemplo de formato: {{"Tipo": "Microcontrolador", "Encapsulado": "LQFP-48", "Voltaje de Operación": "2.0V a 3.6V", "Corriente Máx": "150mA", "Frecuencia": "72MHz", "Pines I/O": "37", "Memoria Flash": "64KB", "Temperatura": "-40 a 85C"}}
    Si no conoces el componente, devuelve: {{"Aviso": "Componente demasiado inusual, requiere verificación manual"}}
    """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        response = model.generate_content(prompt)
        
        texto_limpio = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(texto_limpio)
        
    except Exception as e:
        print(f"Error con la IA: {e}")
        return {"Aviso": "Error al conectar con la Inteligencia Artificial."}

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

class ActualizacionStock(BaseModel):
    nuevo_stock: int

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
    datasheet = f"[https://octopart.com/search?q=](https://octopart.com/search?q=){comp.mpn}"
    
    # Llamada a la IA para obtener las especificaciones reales
    specs_ia = buscar_specs_ia(comp.mpn)
    specs_json = json.dumps(specs_ia)
    
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "INSERT INTO componentes (mpn, nombre, stock, datasheet_url, especificaciones) VALUES (?, ?, ?, ?, ?)",
            (comp.mpn, comp.nombre, comp.stock, datasheet, specs_json)
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
    cursor.execute("SELECT id FROM componentes WHERE id = ?", (componente_id,))
    if not cursor.fetchone():
        conexion.close()
        raise HTTPException(status_code=404, detail="Componente no encontrado")
    cursor.execute("DELETE FROM componentes WHERE id = ?", (componente_id,))
    cursor.execute("DELETE FROM lista_materiales WHERE componente_id = ?", (componente_id,))
    conexion.commit()
    conexion.close()
    return {"mensaje": "Eliminado"}

@app.put("/componentes/{componente_id}/stock")
def actualizar_stock(componente_id: int, datos: ActualizacionStock):
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    cursor.execute("UPDATE componentes SET stock = ? WHERE id = ?", (datos.nuevo_stock, componente_id))
    conexion.commit()
    conexion.close()
    return {"mensaje": "Stock actualizado"}

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
        FROM lista_materiales lm JOIN componentes c ON lm.componente_id = c.id WHERE lm.proyecto_id = ?
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
        if comp_db["stock"] < cant_req:
            conexion.close()
            raise HTTPException(status_code=400, detail=f"Stock insuficiente para MPN: {comp_db['mpn']}")
            
    for mat in materiales:
        cursor.execute("UPDATE componentes SET stock = stock - ? WHERE id = ?", (mat["cantidad_necesaria"], mat["componente_id"]))
        
    conexion.commit()
    conexion.close()
    return {"mensaje": "Ensamblaje exitoso."}