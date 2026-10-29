import os
import uuid

class StorageService:
    @staticmethod
    def save_file(content, extension=".jpg", folder="uploads"):
        """
        Guarda un archivo en el almacenamiento configurado.
        
        Args:
            content (bytes): Contenido del archivo.
            extension (str): Extensión del archivo (incluyendo punto).
            folder (str): Carpeta dentro de 'static' donde guardar.
            
        Returns:
            str: Ruta relativa del archivo guardado (ej: /static/uploads/uuid.jpg).
        """
        filename = f"{uuid.uuid4()}{extension}"
        
        # Definir rutas base
        # Asumimos que la carpeta static está en la raíz del proyecto (TICKETIA_PRO/static)
        # Adaptar si es necesario según estructura.
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # TICKETIA_PRO/
        upload_dir = os.path.join(base_dir, "static", folder)
        
        os.makedirs(upload_dir, exist_ok=True)
        local_path = os.path.join(upload_dir, filename)
        
        # Escribir archivo en disco
        with open(local_path, 'wb') as f:
            f.write(content)
            
        # Devolver path relativo para DB
        return f"/static/{folder}/{filename}"
