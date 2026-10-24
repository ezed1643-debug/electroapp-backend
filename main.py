@app.delete("/componentes/{componente_id}")
def borrar_componente(componente_id: int):
    conexion = sqlite3.connect("electrostock.db")
    cursor = conexion.cursor()
    
    # Verificar si existe
    cursor.execute("SELECT id FROM componentes WHERE id = ?", (componente_id,))
    if not cursor.fetchone():
        conexion.close()
        raise HTTPException(status_code=404, detail="Componente no encontrado")
        
    # Borrarlo
    cursor.execute("DELETE FROM componentes WHERE id = ?", (componente_id,))
    conexion.commit()
    conexion.close()
    
    return {"mensaje": "Componente eliminado con éxito"}