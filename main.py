from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import requests
import json

app = FastAPI()

# ==========================================
# CONFIGURACIÓN DE CORS
# ==========================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# CONFIGURACIÓN DE LA API DE OCTOPART
# ==========================================
# Reemplaza esto con tu API Key gratuita de Nexar (nexar.com)
OCTOPART_API_KEY = "TU_API_KEY_DE_NEXAR_OCTOPART"
OCTOPART_URL = "https://octopart.com/api/v4/endpoint"

# ==========================================
# MODELOS DE DATOS (Pydantic)
# ==========================================
class ProyectoNuevo(BaseModel):
    nombre: str

class MaterialNuevo(BaseModel):
    proyecto_id: int
    componente_id: int
    cantidad: int

class IngresoComponente(BaseModel):
    mpn: str
    nombre: str
    stock: int

# ==========================================
# CONEXIÓN A BASE DE DATOS
# ==========================================
def get_db_connection():
    conn = sqlite3.connect("electrostock.db")
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# RUTAS DEL INVENTARIO
# ==========================================
@app.get("/componentes")
def obtener_inventario():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, mpn, nombre, stock, datasheet_url, especificaciones FROM componentes")
        return [dict(fila) for fila in cursor.fetchall()]
    finally:
        conn.close()

@app.post("/agregar_componente_web")
def agregar_componente_web(comp: IngresoComponente):
    # 1. Consultar a la API de Octopart (GraphQL)
    query = """
    query BuscarComponente($mpn: String!) {
      search(q: $mpn, limit: 1) {
        results {
          part {
            best_datasheet { url }
            specs {
              attribute { name }
              display_value
            }
          }
        }
      }
    }
    """
    try:
        respuesta = requests.post(
            OCTOPART_URL, 
            json={"query": query, "variables": {"mpn": comp.mpn}}, 
            headers={"token": OCTOPART_API_KEY}
        )
        datos = respuesta.json()
        
        resultados = datos.get("data", {}).get("search", {}).get("results", [])
        link_datasheet = None
        especificaciones_json = "{}"
        
        if resultados:
            part_data = resultados[0].get("part", {})
            datasheet = part_data.get("best_datasheet")
            link_datasheet = datasheet.get("url") if datasheet else None
            
            # Extraer especificaciones y convertirlas a JSON
            specs_crudas = part_data.get("specs", [])
            diccionario_specs = {s.get("attribute", {}).get("name"): s.get("display_value") for s in specs_crudas if s.get("attribute", {}).get("name")}
            especificaciones_json = json.dumps(diccionario_specs, ensure_ascii=False)
            
    except Exception as e:
        print(f"Error conectando con Octopart: {e}")
        # Si falla el internet o la API, preparamos datos vacíos para guardarlo igual
        link_datasheet = None
        especificaciones_json = "{}"

    # 2. Guardar el componente en SQLite
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO componentes (mpn, nombre, stock, datasheet_url, especificaciones) 
            VALUES (?, ?, ?, ?, ?)
        """, (comp.mpn, comp.nombre, comp.stock, link_datasheet, especificaciones_json))
        conn.commit()
        return {"mensaje": f"Componente {comp.mpn} agregado exitosamente."}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Este MPN ya existe en el inventario.")
    finally:
        conn.close()

# ==========================================
# RUTAS DE PROYECTOS
# ==========================================
@app.get("/proyectos")
def obtener_proyectos():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, nombre, descripcion FROM proyectos ORDER BY id DESC")
        return [dict(fila) for fila in cursor.fetchall()]
    finally:
        conn.close()

@app.post("/proyectos")
def crear_proyecto(proyecto: ProyectoNuevo):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO proyectos (nombre) VALUES (?)", (proyecto.nombre,))
        conn.commit()
        return {"mensaje": "Proyecto creado exitosamente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error al crear el proyecto.")
    finally:
        conn.close()

# ==========================================
# RUTAS DE LA LISTA DE MATERIALES (BOM)
# ==========================================
@app.get("/lista_materiales/{proyecto_id}")
def obtener_bom(proyecto_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        consulta = """
            SELECT lm.cantidad_necesaria, c.mpn, c.nombre 
            FROM lista_materiales lm
            JOIN componentes c ON lm.componente_id = c.id
            WHERE lm.proyecto_id = ?
        """
        cursor.execute(consulta, (proyecto_id,))
        return [dict(fila) for fila in cursor.fetchall()]
    finally:
        conn.close()

@app.post("/lista_materiales")
def agregar_material(material: MaterialNuevo):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT cantidad_necesaria FROM lista_materiales WHERE proyecto_id = ? AND componente_id = ?", 
                       (material.proyecto_id, material.componente_id))
        existente = cursor.fetchone()
        
        if existente:
            nueva_cantidad = existente["cantidad_necesaria"] + material.cantidad
            cursor.execute("UPDATE lista_materiales SET cantidad_necesaria = ? WHERE proyecto_id = ? AND componente_id = ?", 
                           (nueva_cantidad, material.proyecto_id, material.componente_id))
        else:
            cursor.execute("INSERT INTO lista_materiales (proyecto_id, componente_id, cantidad_necesaria) VALUES (?, ?, ?)", 
                           (material.proyecto_id, material.componente_id, material.cantidad))
        
        conn.commit()
        return {"mensaje": "Componente agregado al proyecto."}
    finally:
        conn.close()

# ==========================================
# RUTA DE ENSAMBLAJE (Restar stock)
# ==========================================
@app.post("/ensamblar_proyecto/{proyecto_id}")
def ensamblar_proyecto(proyecto_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT componente_id, cantidad_necesaria FROM lista_materiales WHERE proyecto_id = ?", (proyecto_id,))
        materiales = cursor.fetchall()
        
        if not materiales:
            raise HTTPException(status_code=400, detail="El proyecto no tiene componentes asignados.")
            
        # 1. Verificar disponibilidad
        for fila in materiales:
            cursor.execute("SELECT stock, nombre FROM componentes WHERE id = ?", (fila["componente_id"],))
            resultado = cursor.fetchone()
            
            if resultado["stock"] < fila["cantidad_necesaria"]:
                raise HTTPException(status_code=400, detail=f"Stock insuficiente para: {resultado['nombre']} (Requerido: {fila['cantidad_necesaria']}, Disponible: {resultado['stock']})")
        
        # 2. Restar el stock
        for fila in materiales:
            cursor.execute("UPDATE componentes SET stock = stock - ? WHERE id = ?", 
                           (fila["cantidad_necesaria"], fila["componente_id"]))
            
        conn.commit()
        return {"mensaje": "Proyecto ensamblado. Stock descontado del inventario."}
    finally:
        conn.close()