from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from typing import Optional, Dict
import firebase_admin
from firebase_admin import credentials, firestore
import hashlib
import os
from datetime import datetime

# Initialize Firebase
cred = credentials.Certificate('key.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize FastAPI
app = FastAPI()

# Helper functions
def serialize_firestore_document(doc):
    data = doc.to_dict()
    data['id'] = doc.id
    for key, value in data.items():
        if isinstance(value, firestore.GeoPoint):
            data[key] = {
                "latitude": value.latitude,
                "longitude": value.longitude
            }
    return data

def hash_password(password):
    salt = os.urandom(16)
    hashed_password = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return salt + hashed_password  # Combine salt and hashed password

def verify_password(password, stored_password):
    salt = stored_password[:16]
    stored_hash = stored_password[16:]
    hashed_password = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return hashed_password == stored_hash

# Helper function to calculate distance between two coordinates (Haversine formula)
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# Pydantic schema
# Product Schema
class ProductSchema(BaseModel):
    name: str
    description: str
    price: float
    quantity: int
    location: Optional[Dict[str, float]]

# User Schema
class UserSchema(BaseModel):
    phone_number: str
    password: Optional[str]
    email: Optional[str]
    name: Optional[str]
    type: str
    description: Optional[str]
    location: Optional[Dict[str, float]] 
    focus_area: Optional[str]

@app.get("/products", response_model=list)
def get_all_products():
    try:
        products = db.collection('products').stream()
        product_list = [serialize_firestore_document(doc) for doc in products]
        return product_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/{product_id}")
def get_product_by_id(product_id: str):
    try:
        doc = db.collection('products').document(product_id).get()
        if doc.exists:
            return {"product": serialize_firestore_document(doc)}
        else:
            raise HTTPException(status_code=404, detail="Product not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/location")
def get_products_by_location(lat: float, lng: float):
    try:
        geo_point = firestore.GeoPoint(lat, lng)
        query = db.collection('products').where('location', '==', geo_point).stream()
        products = [serialize_firestore_document(doc) for doc in query]

        if products:
            return {"products": products}
        else:
            raise HTTPException(status_code=404, detail="No products found at the given location")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/products")
def create_product(product: ProductSchema):
    try:
        product_dict = product.dict()
        if 'location' in product_dict and product_dict['location']:
            loc = product_dict.pop('location')
            product_dict['location'] = firestore.GeoPoint(loc['latitude'], loc['longitude'])
        doc_ref = db.collection('products').add(product_dict)
        return {
            "success": True,
            "message": "Product created successfully",
            "id": doc_ref[1].id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    try:
        doc_ref = db.collection('products').document(product_id)
        doc_ref.delete()
        return {"success": True, "message": "Product deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Register User
@app.route('/users/register', methods=['POST'])
def register_user(user: UserSchema):
    try:  
        # Hash the password if provided
        user_dict = user.dict()
        if user_dict.get("password"):
            user_dict["password"] = hash_password(user_dict["password"])
        
        # Add the user to Firestore
        doc_ref = db.collection('users').add(user_dict)
        return {
            "success": True,
            "message": "User registered successfully",
            "id": doc_ref[1].id
        }, 201
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Find Users by Filters
@app.route('/users/find', methods=['GET'])
def find_users(request: Request):
    query_params = request.query_params
    user_type = query_params('type')
    description_keyword = query_params('description')
    focus_area_keyword = query_params('focus_area')
    lat = query_params('lat', type=float)
    lng = query_params('lng', type=float)
    radius = query_params('radius', type=float)


    query = db.collection('users')
    
    # Filter by user type
    if user_type:
        query = query.where('type', '==', user_type)
    
    # Fetch matching users
    users = query.stream()
    filtered_users = []

    for user in users:
        user_data = user.to_dict()
        
        # Filter by description keyword
        if description_keyword and description_keyword.lower() not in (user_data.get('description', '').lower()):
            continue

        # Filter by focus area
        if focus_area_keyword and focus_area_keyword.lower() not in (user_data.get('focus_area', '').lower()):
            continue

        # Filter by location and radius
        if lat is not None and lng is not None and radius is not None:
            user_location = user_data.get('location')
            if not user_location:
                continue
            
            user_lat = user_location.get('latitude')
            user_lng = user_location.get('longitude')
            
            distance = haversine(lat, lng, user_lat, user_lng)
            if distance > radius:
                continue
            
            user_data['distance'] = distance  # Include distance in the result
        
        filtered_users.append(user_data)
    
    JSONResponse(content=jsonable_encoder(filtered_users), status_code=status.HTTP_200_SUCCESS)

# Find Users by Location Only
@app.route('/find_by_location', methods=['GET'])
def find_users_by_location(request: Request):
    query_params = request.query_params
    lat = query_params('lat', type=float)
    lng = query_params('lng', type=float)
    radius = query_params('radius', type=float, default=10)  # Default to 10 km radius

    if lat is None or lng is None:
        raise HTTPException(status_code=400, detail="No products found at the given location")

    # Fetch users with location data
    users_ref = db.collection('users').where('location', '!=', None).stream()
    nearby_users = []

    for user in users_ref:
        user_data = user.to_dict()
        user_location = user_data.get('location')
        
        if not user_location:
            continue

        user_lat = user_location.get('latitude')
        user_lng = user_location.get('longitude')
        distance = haversine(lat, lng, user_lat, user_lng)

        if distance <= radius:
            user_data['distance'] = distance
            nearby_users.append(user_data)

    # Sort by distance (closest first)
    nearby_users.sort(key=lambda x: x['distance'])

    return JSONResponse(content=jsonable_encoder(nearby_users), status_code=status.HTTP_200_SUCCESS)

# Run the application
if _name_ == "_main_":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)