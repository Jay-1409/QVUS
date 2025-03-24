from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_pymongo import PyMongo
import secrets
import qrcode
import base64
from io import BytesIO
import time
from bson.timestamp import Timestamp  # Import BSON Timestamp
from datetime import datetime       # For createdAt and deletedAt

app = Flask(__name__)

# Configure MongoDB connection (update the URI if needed)
app.config['MONGO_URI'] = 'mongodb://localhost:27017/ev_rental_db'
mongo = PyMongo(app)

# Define collections
users_collection = mongo.db.users
ev_collection = mongo.db.ev
rides_collection = mongo.db.rides
ride_summary_collection = mongo.db.ride_summary  # New collection for ride summaries

# Constant for cost calculation
COST_PER_HOUR = 50# Adjust the rate per hour as needed

# -------------------------------
# Home page
# -------------------------------
@app.route('/')
def index():
    return render_template('index.html')

# -------------------------------
# Registration & EV Booking (assign vehicle tag)
# -------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        phone_number = request.form.get('phone_number')
        
        # Find an available EV (one that is not assigned)
        available_ev = ev_collection.find_one({"is_assigned": False})
        if not available_ev:
            return "No available EV at the moment. Please try again later."
        
        ev_token = available_ev['ev_code']
        
        # Mark the EV as assigned and clear any old ride data
        ev_collection.update_one(
            {"_id": available_ev["_id"]},
            {"$set": {"is_assigned": True, "ride_start_time": None, "ride_end_time": None}}
        )
        
        # Insert user with a createdAt timestamp
        user_data = {
            "username": username,
            "phone_number": phone_number,
            "ev_token": ev_token,
            "createdAt": datetime.utcnow()
        }
        users_collection.insert_one(user_data)
        
        # Generate QR code for the EV token
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(ev_token)
        qr.make(fit=True)
        img = qr.make_image(fill='black', back_color='white')
        
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        return render_template('profile.html', ev_token=ev_token, qr_code=qr_base64)
    
    return render_template('register.html')

# -------------------------------
# Scan and Unlock EV (compare scanned tag with assigned tag)
# -------------------------------
from datetime import datetime

@app.route('/scan', methods=['GET', 'POST'])
def scan():
    if request.method == 'POST':
        username = request.form.get('username')
        scanned_code = request.form.get('scanned_code')
        
        # 1. Validate user
        user = users_collection.find_one({"username": username})
        if not user:
            return jsonify({"success": False, "message": "User not found."})
        
        assigned_ev_code = user.get('ev_token')
        if not assigned_ev_code:
            return jsonify({"success": False, "message": "No EV assigned to this user."})
        
        # 2. Check if scanned code matches the user's EV token
        if scanned_code != assigned_ev_code:
            return jsonify({"success": False, "message": "Scanned EV code does not match the assigned EV."})
        
        # 3. Find EV in the database
        ev = ev_collection.find_one({"ev_code": scanned_code})
        if not ev:
            return jsonify({"success": False, "message": "Invalid EV tag."})
        
        # 4. Ensure the EV is locked before unlocking
        if not ev.get('is_locked', True):
            return jsonify({"success": False, "message": "EV is already unlocked."})
        
        # 5. Unlock the EV
        ev_collection.update_one(
            {"ev_code": scanned_code},
            {"$set": {"is_locked": False}}
        )
        
        # 6. Set ride_start_time in the EV document using a human-readable datetime
        start_time = datetime.utcnow()
        ev_collection.update_one(
            {"ev_code": scanned_code},
            {"$set": {"ride_start_time": start_time}}
        )
        
        # 7. Create a new ride record in the rides collection
        ride_data = {
            "ev_code": scanned_code,
            "user_id": username,         # or user.get("_id") if you store actual user IDs
            "start_time": start_time,
            "end_time": None,
            "createdAt": datetime.utcnow()  # Optional field to track creation time
        }
        rides_collection.insert_one(ride_data)
        
        # 8. Respond with success
        return jsonify({
            "success": True,
            "message": "EV unlocked and ride started!",
            "start_time": str(start_time)
        })
    
    return render_template('scan.html')

#--------------------------------
# DROP THE VEHICLE
# -------------------------------
@app.route('/return_ev', methods=['GET'])
def return_ev():
    return render_template('drop_vehicle.html')

@app.route('/drop_vehicle', methods=['POST'])
def drop_vehicle():
    username = request.form.get('username')
    
    # Find the user by username.
    user = users_collection.find_one({"username": username})
    if not user:
        return "User not found", 404
    
    stored_ev_code = user.get("ev_token")
    if not stored_ev_code:
        return "No EV is currently assigned to this user.", 400

    ev = ev_collection.find_one({"ev_code": stored_ev_code})
    if not ev or not ev.get("ride_start_time"):
        return "Ride start time not recorded for this EV.", 400

    # Set the ride_end_time right now.
    ride_end_time = datetime.utcnow()
    
    # Update the EV document to set the ride_end_time.
    ev_collection.update_one(
        {"ev_code": stored_ev_code},
        {"$set": {"ride_end_time": ride_end_time}}
    )
    
    # Calculate ride duration (in seconds) using the ride_start_time from the EV record.
    duration_seconds = (ride_end_time - ev["ride_start_time"]).total_seconds()
    hours = duration_seconds / 3600.0
    total_cost = COST_PER_HOUR * hours

    # Prepare the ride summary data.
    summary_data = {
        "ev_number": stored_ev_code,
        "user_id": user.get("_id"),
        "phone_number": user.get("phone_number"),
        "total_cost": total_cost,
        "ride_duration_hours": hours,
        "ride_start_time": ev["ride_start_time"],
        "ride_end_time": ride_end_time,
        "createdAt": datetime.utcnow()  # Timestamp for when the summary is created.
    }
    summary_result = ride_summary_collection.insert_one(summary_data)
    print(f"Ride summary inserted with ID: {summary_result.inserted_id}")

    # Update the EV document: mark as locked and not assigned, and reset ride times.
    ev_collection.update_one(
        {"ev_code": stored_ev_code},
        {"$set": {"is_locked": True, "is_assigned": False, "ride_start_time": None, "ride_end_time": None}}
    )
    
    # Update the user document with a deletedAt timestamp before deletion.
    users_collection.update_one(
        {"username": username},
        {"$set": {"deletedAt": datetime.utcnow()}}
    )
    
    # Delete the user from the users_collection.
    user_delete_result = users_collection.delete_one({"username": username})
    
    if user_delete_result.deleted_count == 1:
        message = (
            "EV dropped successfully, your account has been removed, and a ride summary has been saved. "
            f"Total cost for your ride is ${total_cost:.2f}"
        )
        return render_template('drop_success.html', message=message)
    else:
        return "Failed to delete the user", 500

if __name__ == '__main__':
    app.run(debug=True)
