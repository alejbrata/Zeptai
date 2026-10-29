import os
from datetime import datetime
from core.db_models import db, GeneratedDocument, ActivityLog

class DocumentService:
    @staticmethod
    def save_generated_document(user_phone, file_path, doc_type, client_name="Documento IA", description=None):
        try:
            # Fix path format if it's external or relative
            if not file_path.startswith('/static') and not file_path.startswith('http'):
                file_path = f"/static/generated_docs/{os.path.basename(file_path)}"
            
            new_doc = GeneratedDocument(
                user_phone=user_phone,
                file_path=file_path,
                doc_type=doc_type,
                client_name=client_name,
                created_at=datetime.utcnow()
            )
            db.session.add(new_doc)
            db.session.commit()
            
            log_msg = f"Generado {doc_type}: {client_name}"
            if description: log_msg += f" ({description})"
            
            ActivityLog.log(user_phone, "System", log_msg)
            return new_doc.id
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error saving GeneratedDocument: {e}")
            return None