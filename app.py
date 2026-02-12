import os
import requests
import logging
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Charger les variables d'environnement (.env en local, Variables Render en prod)
load_dotenv()

# --- CONFIGURATION LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- CONFIGURATION BASE DE DONNÉES (POSTGRESQL) ---
db_url = os.getenv('DATABASE_URL')

if not db_url:
    logger.error("❌ Erreur : DATABASE_URL n'est pas définie dans l'environnement !")
else:
    # Correction indispensable pour Render et SQLAlchemy 1.4+
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SCHEDULER_API_ENABLED'] = False

# Identifiants Learnybox
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

# --- INITIALISATION AUTOMATIQUE (LOCAL & PROD) ---
# Ce bloc s'exécute au chargement de l'app par Flask ou Gunicorn
with app.app_context():
    try:
        db.create_all()
        logger.info("✅ Base de données PostgreSQL synchronisée.")
    except Exception as e:
        logger.error(f"❌ Erreur lors de la création des tables : {e}")

if not scheduler.running:
    scheduler.init_app(app)
    scheduler.start()
    logger.info("⏰ Scheduler démarré.")

# ---------------------------------------------------------
# LOGIQUE MÉTIER
# ---------------------------------------------------------

def get_fresh_learny_token():
    logger.info("🔑 Demande d'un nouveau Token Learnybox...")
    try:
        headers = {"X-API-Key": LEARNY_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"grant_type": "access_token"}
        resp = requests.post(LEARNY_TOKEN_URL, headers=headers, data=payload, timeout=15)
        if resp.status_code == 200:
            token = resp.json().get('data', {}).get('access_token')
            if token: return token
        logger.error(f"❌ Erreur API Token : {resp.text}")
        return None
    except Exception as e:
        logger.error(f"❌ Exception Token : {e}")
        return None

@scheduler.task('interval', id='process_daily_sequence', hours=1)

def process_daily_sequence():
    """Vérifie les sessions passées (J+1) et inscrit à la séquence."""
    with app.app_context():
        target_date = (datetime.now() - timedelta(days=1)).date()
        logger.info(f"--- 🕒 SCAN : Sessions jusqu'au {target_date} ---")
        
        users_to_process = User.query.filter(
            User.session_date <= target_date, 
            User.status == 'pending'
        ).all()
        
        if not users_to_process:
            logger.info("RAS : Personne à traiter.")
            return

        token = get_fresh_learny_token()
        if not token: return

        for user in users_to_process:
            logger.info(f"🚀 Inscription J+1 : {user.email}")
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "prenom": user.firstname, "nom": user.lastname, "email": user.email,
                "mobile": user.phone, "id_sequence": user.sequence_id, "rgpd": 1, "rgpd_date": datetime.now().strftime('%Y-%m-%d')
            }
            try:
                r = requests.post(LEARNY_CONTACT_URL, headers=headers, data=payload, timeout=15)
                if r.status_code in [200, 201]:
                    user.status = 'processed'
                    logger.info(f"✅ Succès : {user.email}")
                else:
                    logger.warning(f"⚠️ Échec : {user.email} -> {r.text}")
                    user.status = 'error'
            except Exception as e:
                logger.error(f"❌ Erreur réseau pour {user.email} : {e}")
        
        db.session.commit()

# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    if not data or 'email' not in data or 'session_date' not in data:
        return jsonify({"status": "error", "message": "Données incomplètes"}), 400
    try:
        webinar_date = datetime.strptime(data['session_date'], '%Y-%m-%d').date()
        new_user = User(
            email=data['email'], firstname=data.get('firstname'), lastname=data.get('lastname'),
            phone=data.get('phone'), sequence_id=data.get('sequence_id'), session_date=webinar_date
        )
        db.session.add(new_user)
        db.session.commit()
        logger.info(f"📥 Reçu : {new_user.email}")
        return jsonify({"status": "success"}), 201
    except Exception as e:
        logger.error(f"❌ Erreur register : {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/health')
def health():
    return "OK", 200

# Démarrage local
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    # use_reloader=False impératif pour éviter le double lancement du scheduler en local
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)