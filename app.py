import os
import secrets
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, Restaurant, Category, Dish, Order, OrderItem
from datetime import datetime, date

app = Flask(__name__)
CORS(app)

# === Configuration ===
# ⚠️ En production sur Railway, DATABASE_URL est fourni automatiquement
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Railway fournit une URL postgres:// → SQLAlchemy 1.4+ requiert postgresql://
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url.replace("postgres://", "postgresql://", 1)
else:
    # Mode local uniquement (développement)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/database.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

db.init_app(app)

# === Utilitaires ===
def generate_public_id():
    return "rest_" + secrets.token_urlsafe(8).replace("_", "").replace("-", "")[:8]

def get_restaurant_by_public_id(public_id):
    return Restaurant.query.filter_by(public_id=public_id).first_or_404()

def extract_price_from_string(price_str):
    match = re.search(r'[\d.]+', price_str)
    return float(match.group()) if match else 0.0

def get_or_create_category(restaurant_id, category_name):
    category = Category.query.filter_by(
        restaurant_id=restaurant_id,
        name=category_name
    ).first()
    if not category:
        category = Category(name=category_name, restaurant_id=restaurant_id)
        db.session.add(category)
        db.session.flush()
    return category

def format_orders_for_staff(orders):
    result = []
    for order in orders:
        items = [{"name": item.dish.name, "price": f"{item.dish.price} MAD"} for item in order.items]
        total = sum(item.dish.price * item.quantity for item in order.items)
        result.append({
            "id": order.id,
            "table_number": order.table_number or "—",
            "items": items,
            "total_price": total,
            "timestamp": order.created_at.isoformat()
        })
    return result

# === Routes ===
db.init_app(app)

# Crée les tables au démarrage
with app.app_context():
    db.create_all()

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

    # ✅ Correction : suppression des espaces dans les URLs par défaut
    client_url = f"{os.environ.get('CLIENT_URL', 'https://client.example.com').rstrip('/')}/client/{public_id}"
    staff_url = f"{os.environ.get('STAFF_URL', 'https://staff.example.com').rstrip('/')}/staff/{public_id}"

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
        "image_data": dish.image_base64 or ""
    } for dish, category_name in dishes])

@app.route('/api/menu/add/<public_id>', methods=['POST'])
def add_dish(public_id):
    restaurant = get_restaurant_by_public_id(public_id)
    data = request.get_json()
    name = data.get('name')
    desc = data.get('description')
    category_name = data.get('category')
    price_str = data.get('price')
    image_b64 = data.get('image_data')

    if not all([name, desc, category_name, price_str]):
        return jsonify({'error': 'Champs manquants'}), 400

    try:
        price = extract_price_from_string(price_str)
    except:
        return jsonify({'error': 'Prix invalide'}), 400

    category = get_or_create_category(restaurant.id, category_name)
    dish = Dish(name=name, description=desc, price=price, image_base64=image_b64,
                category_id=category.id, restaurant_id=restaurant.id)
    db.session.add(dish)
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

@app.route('/health')
def health():
    return {'status': 'ok'}

# === Lancement ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)