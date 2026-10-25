from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json
import requests
import os
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def conectar_db():
    conexion = sqlite3.connect("electrostock.db", timeout=20.0)
    conexion.execute("PRAGMA journal_mode=WAL;")
    return conexion

def iniciar_db():
    conexion = conectar_db()
    cursor = conexion.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS componentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mpn TEXT UNIQUE NOT NULL, nombre TEXT NOT NULL,
            stock INTEGER NOT NULL, datasheet_url TEXT, especificaciones TEXT
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

# --- EXTRACCIÓN INTELIGENTE CON IA (AUTO-DESCUBRIMIENTO) ---
def buscar_specs_ia(mpn: str):
    clave_secreta = os.getenv("GEMINI_API_KEY") 
    if not clave_secreta: return {"Aviso": "Error: GEMINI_API_KEY no configurada."}
    
    # ⚡ PASO 1: LE PREGUNTAMOS A GOOGLE QUÉ MODELOS TIENES PERMITIDOS ⚡
    url_modelos = f"https://generativelanguage.googleapis.com/v1beta/models?key={clave_secreta}"
    try:
        res_modelos = requests.get(url_modelos, timeout=15)
        if res_modelos.status_code != 200:
            return {"Aviso": "Tu API Key es inválida o fue bloqueada.", "Detalle": res_modelos.text}
        
        datos = res_modelos.json()
        # Filtramos solo los modelos que sirven para generar texto (generateContent)
        modelos_disponibles = [m["name"].split("/")[-1] for m in datos.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        
        if not modelos_disponibles:
            return {"Aviso": "Tu API Key no tiene ningún modelo de texto habilitado por Google."}
            
        # Elegimos el mejor modelo (Prioridad: flash > pro > cualquiera que funcione)
        modelo_elegido = modelos_disponibles[0]
        for m in modelos_disponibles:
            if "gemini-1.5-flash" in m:
                modelo_elegido = m
                break
            elif "gemini-1.0-pro" in m:
                modelo_elegido = m
                
    except Exception as e:
        return {"Aviso": "Fallo al consultar permisos a Google.", "Detalle": str(e)}

    # ⚡ PASO 2: EXTRAEMOS LOS DATOS USANDO EL MODELO QUE GOOGLE AUTORIZÓ ⚡
    prompt = f"""Eres un ingeniero electrónico experto. Necesito hasta 10 especificaciones técnicas clave del componente '{mpn}'. Devuelve ÚNICAMENTE un objeto JSON válido. No uses markdown. Ejemplo: {{"Tipo": "Microcontrolador", "Voltaje": "3.3V"}}"""
    
    url_segura = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_elegido}:generateContent?key={clave_secreta}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    ultimo_error = "Desconocido"
    for intento in range(3):
        try:
            respuesta = requests.post(url_segura, json=payload, headers=headers, timeout=25)
            
            if respuesta.status_code == 200:
                texto = respuesta.json()["candidates"][0]["content"]["parts"][0]["text"]
                texto_limpio = texto.replace('```json', '').replace('```', '').strip()
                try:
                    return json.loads(texto_limpio)
                except Exception as e:
                    return {"Aviso": "Formato de IA inválido.", "Modelo": modelo_elegido, "Texto": texto_limpio}
            elif respuesta.status_code == 503:
                ultimo_error = "Error 503: Google saturado."
                time.sleep(2)
                continue
            else: 
                return {"Aviso": f"Error HTTP {respuesta.status_code}", "Modelo_Usado": modelo_elegido, "Detalle": respuesta.text}
                
        except requests.exceptions.Timeout:
            ultimo_error = "Timeout: Render cortó la conexión."
        except Exception as e: 
            ultimo_error = f"Fallo de red: {str(e)}"
            
    return {"Aviso": "Fallo definitivo de IA", "Modelo_Intentado": modelo_elegido, "Causa_Real": ultimo_error}

# Función que corre en segundo plano sin interrumpir al usuario
def actualizar_specs_db(comp_id: int, mpn: str):
    specs = buscar_specs_ia(mpn)
    specs_json = json.dumps(specs)
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("UPDATE componentes SET especificaciones = ? WHERE id = ?", (specs_json, comp_id))
        conexion.commit()
    finally:
        conexion.close()

class ComponenteWeb(BaseModel): mpn: str; nombre: str; stock: int
class Proyecto(BaseModel): nombre: str
class ComponenteProyecto(BaseModel): proyecto_id: int; componente_id: int; cantidad: int
class ActualizacionStock(BaseModel): nuevo_stock: int

@app.get("/componentes")
def obtener_componentes():
    conexion = conectar_db()
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM componentes")
    componentes = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return componentes

@app.post("/agregar_componente_web")
def agregar_componente_web(comp: ComponenteWeb, background_tasks: BackgroundTasks):
    datasheet = f"https://www.alldatasheet.com/view.jsp?Searchword={comp.mpn}"
    
    # Texto temporal que se guarda al instante
    specs_json = json.dumps({"IA": "Extrayendo datos en segundo plano... Vuelve a abrir esta ventana en unos segundos."})
    
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        # Se guarda el componente inmediatamente (0.1 segundos)
        cursor.execute("INSERT INTO componentes (mpn, nombre, stock, datasheet_url, especificaciones) VALUES (?, ?, ?, ?, ?)", (comp.mpn, comp.nombre, comp.stock, datasheet, specs_json))
        comp_id = cursor.lastrowid
        conexion.commit()
        
        # ⚡ INICIAMOS LA IA EN SEGUNDO PLANO ⚡
        background_tasks.add_task(actualizar_specs_db, comp_id, comp.mpn)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="El componente ya existe.")
    finally: 
        conexion.close()
        
    return {"mensaje": "Éxito"}

@app.delete("/componentes/{componente_id}")
def borrar_componente(componente_id: int):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("DELETE FROM componentes WHERE id = ?", (componente_id,))
        cursor.execute("DELETE FROM lista_materiales WHERE componente_id = ?", (componente_id,))
        conexion.commit()
    finally: conexion.close()
    return {"mensaje": "Eliminado"}

@app.put("/componentes/{componente_id}/stock")
def actualizar_stock(componente_id: int, datos: ActualizacionStock):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("UPDATE componentes SET stock = ? WHERE id = ?", (datos.nuevo_stock, componente_id))
        conexion.commit()
    finally: conexion.close()
    return {"mensaje": "Actualizado"}

@app.get("/proyectos")
def obtener_proyectos():
    conexion = conectar_db()
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM proyectos")
    proyectos = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return proyectos

@app.post("/proyectos")
def crear_proyecto(proy: Proyecto):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("INSERT INTO proyectos (nombre) VALUES (?)", (proy.nombre,))
        conexion.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Ya existe")
    finally: conexion.close()
    return {"mensaje": "Creado"}

@app.delete("/proyectos/{proyecto_id}")
def borrar_proyecto(proyecto_id: int):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("DELETE FROM lista_materiales WHERE proyecto_id = ?", (proyecto_id,))
        cursor.execute("DELETE FROM proyectos WHERE id = ?", (proyecto_id,))
        conexion.commit()
    finally: conexion.close()
    return {"mensaje": "Proyecto eliminado"}

@app.get("/lista_materiales/{proyecto_id}")
def obtener_bom(proyecto_id: int):
    conexion = conectar_db()
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute('''SELECT lm.rowid AS id, c.mpn, c.nombre, lm.cantidad_necesaria, c.stock 
                      FROM lista_materiales lm JOIN componentes c ON lm.componente_id = c.id 
                      WHERE lm.proyecto_id = ?''', (proyecto_id,))
    bom = [dict(row) for row in cursor.fetchall()]
    conexion.close()
    return bom

@app.post("/lista_materiales")
def agregar_a_bom(item: ComponenteProyecto):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        # Buscamos si el componente ya está en el proyecto
        cursor.execute("SELECT rowid, cantidad_necesaria FROM lista_materiales WHERE proyecto_id = ? AND componente_id = ?", (item.proyecto_id, item.componente_id))
        registro = cursor.fetchone()
        
        if registro:
            # Si existe, sumamos la cantidad
            nueva_cantidad = registro[1] + item.cantidad
            cursor.execute("UPDATE lista_materiales SET cantidad_necesaria = ? WHERE rowid = ?", (nueva_cantidad, registro[0]))
        else:
            # Si no existe, lo creamos
            cursor.execute("INSERT INTO lista_materiales (proyecto_id, componente_id, cantidad_necesaria) VALUES (?, ?, ?)", (item.proyecto_id, item.componente_id, item.cantidad))
            
        conexion.commit()
    finally: 
        conexion.close()
    return {"mensaje": "Lista de materiales actualizada"}

@app.delete("/lista_materiales/{item_id}")
def quitar_de_bom(item_id: int):
    conexion = conectar_db()
    cursor = conexion.cursor()
    try:
        cursor.execute("DELETE FROM lista_materiales WHERE rowid = ?", (item_id,))
        conexion.commit()
    finally: conexion.close()
    return {"mensaje": "Removido"}

@app.post("/ensamblar_proyecto/{proyecto_id}")
def ensamblar_proyecto(proyecto_id: int):
    conexion = conectar_db()
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    try:
        cursor.execute("SELECT componente_id, cantidad_necesaria FROM lista_materiales WHERE proyecto_id = ?", (proyecto_id,))
        materiales = cursor.fetchall()
        if not materiales: raise HTTPException(status_code=400, detail="Sin componentes.")
        for mat in materiales:
            cursor.execute("SELECT stock, mpn FROM componentes WHERE id = ?", (mat["componente_id"],))
            comp_db = cursor.fetchone()
            if comp_db["stock"] < mat["cantidad_necesaria"]:
                raise HTTPException(status_code=400, detail=f"Stock insuficiente para {comp_db['mpn']}")
        for mat in materiales:
            cursor.execute("UPDATE componentes SET stock = stock - ? WHERE id = ?", (mat["cantidad_necesaria"], mat["componente_id"]))
        conexion.commit()
    finally: conexion.close()
    return {"mensaje": "Ensamblaje exitoso."}