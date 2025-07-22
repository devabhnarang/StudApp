from werkzeug.security import generate_password_hash
import MySQLdb


db_host = "localhost"
db_user = "root"
db_password = "devabh"
db_name = "studapp"


admin_name = "Admin"
admin_email = "admin@studapp"
admin_password = "devabh"
admin_role = "admin"

try:
   
    conn = MySQLdb.connect(host=db_host, user=db_user, passwd=db_password, db=db_name)
    cur = conn.cursor()

  
    cur.execute("SELECT id FROM users WHERE email = %s", (admin_email,))
    if cur.fetchone():
        print("Admin user already exists.")
    else:
      
        hashed_password = generate_password_hash(admin_password)

    
        cur.execute(
            "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
            (admin_name, admin_email, hashed_password, admin_role)
        )
        conn.commit()
        print("✅ Admin user added successfully!")

except Exception as e:
    print("❌ Error:", e)

finally:
    if conn:
        cur.close()
        conn.close()
