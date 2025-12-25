import os
import secrets
import re
import requests
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, Restaurant, Category, Dish, Order, OrderItem
from datetime import datetime, date

app = Flask(__name__)
CORS(app)

# Configuration de la base de données
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///instance/database.db'
).replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# Initialisation de la base de données
db.init_app(app)

# Création des tables au démarrage
with app.app_context():
    db.create_all()


# === UTILITAIRES ===
def generate_public_id():
    return "rest_" + secrets.token_urlsafe(8).replace("_", "").replace("-", "")[:8]

def get_restaurant_by_public_id(public_id):
    return Restaurant.query.filter_by(public_id=public_id).first_or_404()

def extract_price_from_string(price_str):
    match = re.search(r'[\d.]+', price_str)
    return float(match.group()) if match else 0.0

def get_or_create_category(restaurant_id, category_name):
    category = Category.query.filter_by(restaurant_id=restaurant_id, name=category_name).first()
    if not category:
        category = Category(name=category_name, restaurant_id=restaurant_id)
        db.session.add(category)
        db.session.flush()
    return category

def format_orders_for_staff(orders):
    """Formatte les commandes pour l'affichage côté staff, avec gestion des quantités."""
    result = []
    for order in orders:
        items = []
        total = 0
        for item in order.items:
            price = item.dish.price
            qty = item.quantity
            total += price * qty
            name_display = item.dish.name
            if qty > 1:
                name_display += f" (x{qty})"
            items.append({
                "name": name_display,
                "price": f"{price} MAD"
            })
        result.append({
            "id": order.id,
            "table_number": order.table_number or "—",
            "items": items,
            "total_price": round(total, 2),
            "timestamp": order.created_at.isoformat()
        })
    return result

def upload_image_to_supabase(image_b64, public_id, dish_id):
    """Upload une image à Supabase Storage"""
    supabase_url = os.environ.get("SUPABASE_URL")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    
    if not image_b64 or not supabase_url or not service_role_key:
        return None

    try:
        # Gérer le format image/... ou pur Base64
        if image_b64.startswith('data:image/'):
            header, encoded = image_b64.split(",", 1)
            content_type = header.split(";")[0].split(":")[1]
            file_ext = content_type.split("/")[1]
        else:
            encoded = image_b64
            content_type = "image/jpeg"
            file_ext = "jpg"

        # Décoder le Base64
        file_data = base64.b64decode(encoded)

        # Nom unique
        file_name = f"dish_{dish_id}.{file_ext}"

        # URL Supabase
        url = f"{supabase_url}/storage/v1/object/menu-images/{public_id}/{file_name}"
        headers = {
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": content_type
        }

        response = requests.post(url, headers=headers, data=file_data)
        
        if response.status_code == 200:
            return f"menu-images/{public_id}/{file_name}"
        else:
            print(f"Erreur Supabase upload: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"Exception upload: {str(e)}")
        return None


# === ROUTES ===
@app.route('/api/register', methods=['POST'])
def register_restaurant():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    if not name:
        return jsonify({'error': 'Nom requis'}), 400
    if Restaurant.query.filter_by(name=name).first():
        return jsonify({'error': 'Nom déjà utilisé'}), 409

    public_id = generate_public_id()
    restaurant = Restaurant(name=name, email=email, public_id=public_id)
    db.session.add(restaurant)
    db.session.commit()

    # ✅ CORRIGÉ : pas d'espaces dans les URLs par défaut
    client_url_base = os.getenv("CLIENT_URL", "https://client.example.com").rstrip('/')
    staff_url_base = os.getenv("STAFF_URL", "https://staff.example.com").rstrip('/')

    client_url = f"{client_url_base}/?token={public_id}"
    staff_url = f"{staff_url_base}/dashboard.html?token={public_id}"

    return jsonify({
        'restaurant_id': public_id,
        'client_url': client_url,
        'staff_url': staff_url
    }), 201


@app.route('/api/menu/<public_id>', methods=['GET'])
def get_menu_flat(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    dishes = db.session.query(Dish, Category.name.label('category_name')) \
        .join(Category, Dish.category_id == Category.id) \
        .filter(Dish.restaurant_id == restaurant.id).all()
    return jsonify([{
        "id": dish.id,
        "name": dish.name,
        "description": dish.description or "Délicieux plat de notre maison.",
        "price": f"{dish.price} MAD",
        "category": category_name,
        "image_path": dish.image_path or ""  # ✅ Retourne le chemin, pas le Base64
    } for dish, category_name in dishes])


@app.route('/api/menu/add/<public_id>', methods=['POST'])
def add_dish(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    name = request.form.get('name')
    desc = request.form.get('description')
    category_name = request.form.get('category')
    price_str = request.form.get('price')
    image_file = request.files.get('image_data')

    if not all([name, desc, category_name, price_str]):
        return jsonify({'error': 'Champs manquants'}), 400

    try:
        price = extract_price_from_string(price_str)
    except Exception:
        return jsonify({'error': 'Prix invalide'}), 400

    category = get_or_create_category(restaurant.id, category_name)
    
    # Créer le plat (sans image pour avoir un ID)
    dish = Dish(name=name, description=desc, price=price,
                category_id=category.id, restaurant_id=restaurant.id)
    db.session.add(dish)
    db.session.flush()  # Pour obtenir l'ID

    # Upload l'image si présente
    image_path = None
    if image_file:
        # Convertir le fichier en Base64
        image_b64 = f"data:image/{image_file.content_type.split('/')[1]};base64,{base64.b64encode(image_file.read()).decode('utf-8')}"
        image_path = upload_image_to_supabase(image_b64, public_id, dish.id)
    
    dish.image_path = image_path
    db.session.commit()
    
    return jsonify({'id': dish.id}), 201


@app.route('/api/menu/<int:dish_id>', methods=['DELETE'])
def delete_dish(dish_id):
    dish = Dish.query.get_or_404(dish_id)
    db.session.delete(dish)
    db.session.commit()
    return jsonify({'success': True}), 200


@app.route('/api/orders/pending/<public_id>', methods=['GET'])
def get_pending_orders(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    orders = Order.query.filter_by(restaurant_id=restaurant.id, status='pending') \
        .order_by(Order.created_at.desc()).all()
    return jsonify(format_orders_for_staff(orders))


@app.route('/api/orders/confirmed/<public_id>', methods=['GET'])
def get_confirmed_orders(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    orders = Order.query.filter(
        Order.restaurant_id == restaurant.id,
        Order.status.in_(['validated', 'completed'])
    ).order_by(Order.created_at.desc()).all()
    return jsonify(format_orders_for_staff(orders))


@app.route('/api/order/<int:order_id>/confirm', methods=['POST'])
def confirm_order(order_id):
    order = Order.query.get_or_404(order_id)
    order.status = 'validated'
    db.session.commit()
    return jsonify({'success': True}), 200


@app.route('/api/order/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    order = Order.query.get_or_404(order_id)
    db.session.delete(order)
    db.session.commit()
    return jsonify({'success': True}), 200


@app.route('/api/stats/today/<public_id>', methods=['GET'])
def get_stats_today(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    today = date.today()
    orders = Order.query.filter(
        Order.restaurant_id == restaurant.id,
        db.cast(Order.created_at, db.Date) == today,
        Order.status.in_(['validated', 'completed'])
    ).all()
    total_sales = sum(sum(item.dish.price * item.quantity for item in order.items) for order in orders)
    return jsonify({'total_sales': round(total_sales, 2), 'orders_count': len(orders)})


@app.route('/api/order/<public_id>', methods=['POST'])
def create_order_client(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    data = request.get_json()
    table_number = data.get('table_number')
    items = data.get('items', [])
    if not items:
        return jsonify({'error': 'Aucun plat sélectionné'}), 400

    order = Order(restaurant_id=restaurant.id, table_number=str(table_number))
    db.session.add(order)
    db.session.flush()

    for item in items:
        dish = Dish.query.filter_by(id=item['id'], restaurant_id=restaurant.id).first()
        if not dish:
            db.session.rollback()
            return jsonify({'error': f'Plat non trouvé: {item["id"]}'}, 400)
        oi = OrderItem(order_id=order.id, dish_id=dish.id, quantity=1)
        db.session.add(oi)

    db.session.commit()
    return jsonify({'order_id': order.id}), 201


@app.route('/api/order/<int:order_id>/status', methods=['GET'])
def get_order_status_client(order_id):
    order = Order.query.get_or_404(order_id)
    status = 'confirmed' if order.status in ['validated', 'completed'] else 'pending'
    return jsonify({'status': status})


# === Routes utilitaires ===
@app.route('/health')
def health():
    return {'status': 'ok'}

@app.route('/debug-env')
def debug_env():
    return jsonify({
        "CLIENT_URL": os.getenv("CLIENT_URL"),
        "STAFF_URL": os.getenv("STAFF_URL"),
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "")[:60] + "...",
        "DATABASE_URL": (os.getenv("DATABASE_URL") or "")[:60] + ("..." if os.getenv("DATABASE_URL") and len(os.getenv("DATABASE_URL")) > 60 else ""),
    })

@app.route('/')
def index():
    return "✅ Backend fonctionnel ! Accédez aux endpoints via /api/..."


# === Démarrage ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)