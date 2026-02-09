import os
import requests
import logging
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Charger les variables d'environnement depuis .env
load_dotenv()

# --- CONFIGURATION LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- CONFIGURATION APP ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///registrations.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SCHEDULER_API_ENABLED'] = False # Sécurité : désactive l'API publique du scheduler

# Constantes Learnybox depuis .env
LEARNY_API_KEY = os.getenv('LEARNY_API_KEY')
LEARNY_TOKEN_URL = os.getenv('LEARNY_TOKEN_URL')
LEARNY_CONTACT_URL = os.getenv('LEARNY_CONTACT_URL')

db = SQLAlchemy(app)
scheduler = APScheduler()

# --- MODÈLE UTILISATEUR ---
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    firstname = db.Column(db.String(100))
    lastname = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    sequence_id = db.Column(db.Integer)
    session_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ---------------------------------------------------------
# SERVICES EXTERNES
# ---------------------------------------------------------
def get_fresh_learny_token():
    """Récupère un nouveau token Learnybox."""
    logger.info("🔑 Demande d'un nouveau Token Learnybox...")
    try:
        headers = {
            "X-API-Key": LEARNY_API_KEY, 
            "Content-Type": "application/x-www-form-urlencoded"
        }
        payload = {"grant_type": "access_token"}
        resp = requests.post(LEARNY_TOKEN_URL, headers=headers, data=payload, timeout=15)
        
        if resp.status_code == 200:
            token = resp.json().get('data', {}).get('access_token')
            if token:
                logger.info("✅ Token généré avec succès.")
                return token
        
        logger.error(f"❌ Erreur API Token : {resp.text}")
        return None
    except Exception as e:
        logger.error(f"❌ Exception lors de l'appel Token : {e}")
        return None

# ---------------------------------------------------------
# LOGIQUE AUTOMATIQUE (SCHEDULER)
# ---------------------------------------------------------
@scheduler.task('interval', id='process_daily_sequence', hours=1)
def process_daily_sequence():
    """
    Vérifie et déclenche les inscriptions à J+1 de la session.
    S'exécute toutes les heures pour garantir la livraison même en cas de coupure.
    """
    with app.app_context():
        # La cible est "Hier" : si la session était hier, aujourd'hui c'est J+1.
        target_date = (datetime.now() - timedelta(days=1)).date()
        
        logger.info(f"--- 🕒 SCAN STABLE : Sessions jusqu'au {target_date} ---")
        
        # LOGIQUE DE SÉCURITÉ :
        # On utilise <= target_date pour attraper les sessions d'hier ET celles 
        # qui auraient pu être ratées avant (si le serveur était éteint par exemple).
        users_to_process = User.query.filter(
            User.session_date <= target_date, 
            User.status == 'pending'
        ).all()
        
        if not users_to_process:
            logger.info("RAS : Personne à traiter pour le moment.")
            return

        # On ne génère le token que s'il y a des gens à traiter
        token = get_fresh_learny_token()
        if not token:
            logger.error("❌ Abandon : Impossible d'obtenir un token Learnybox.")
            return

        for user in users_to_process:
            logger.info(f"🚀 Inscription Différée (J+1+) : {user.email}")
            
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "prenom": user.firstname,
                "nom": user.lastname,
                "email": user.email,
                "mobile": user.phone,
                "id_sequence": user.sequence_id,
                "rgpd": 1
            }
            
            try:
                # Ajout d'un timeout pour éviter que le script ne reste bloqué
                r = requests.post(LEARNY_CONTACT_URL, headers=headers, data=payload, timeout=15)
                
                if r.status_code in [200, 201]:
                    user.status = 'processed'
                    logger.info(f"✅ Succès : {user.email} ajouté à la séquence.")
                else:
                    # En cas d'erreur API, on marque 'error' mais on pourrait aussi 
                    # laisser en 'pending' pour réessayer l'heure suivante.
                    logger.warning(f"⚠️ Échec API pour {user.email} : {r.text}")
                    user.status = 'error'
                    
            except Exception as e:
                logger.error(f"❌ Erreur réseau pour {user.email} : {e}")
                # On ne change pas le statut ici pour permettre de réessayer au prochain scan d'une heure
        
        # Enregistrement final des changements dans la base SQLite
        db.session.commit()
        logger.info("--- Fin du scan stable ---")

# ---------------------------------------------------------
# ROUTES API
# ---------------------------------------------------------
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    if not data or 'email' not in data or 'session_date' not in data:
        return jsonify({"status": "error", "message": "Données incomplètes"}), 400
        
    try:
        webinar_date = datetime.strptime(data['session_date'], '%Y-%m-%d').date()
        
        new_user = User(
            email=data['email'],
            firstname=data['firstname'],
            lastname=data['lastname'],
            phone=data.get('phone'),
            sequence_id=data.get('sequence_id'),
            session_date=webinar_date
        )
        db.session.add(new_user)
        db.session.commit()
        
        logger.info(f"📥 Nouveau contact en file d'attente : {new_user.email}")
        return jsonify({"status": "success", "message": "Enregistré pour J+1"}), 201
        
    except Exception as e:
        logger.error(f"❌ Erreur register : {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------------------------------------
# DÉMARRAGE
# ---------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    scheduler.init_app(app)
    scheduler.start()
    
    # Render utilise la variable PORT
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)