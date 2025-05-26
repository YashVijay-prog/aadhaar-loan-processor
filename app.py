from flask import Flask, request, render_template, redirect, url_for, jsonify, flash, session
from werkzeug.utils import secure_filename
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError
import time
import pandas as pd
import os
import threading
import logging
import re
import json
import uuid
import random

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Required for session and flash messages
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'Uploads')
app.config['SCREENSHOT_FOLDER'] = os.path.join(os.getcwd(), 'screenshots')
app.config['TEMP_FOLDER'] = os.path.join(os.getcwd(), 'temp')  # New folder for temp files
app.config['LOG_FILE'] = os.path.join(os.getcwd(), 'process.log')

# Configuration
SHEET_NAME = "Sheet1"
FINANCIAL_YEAR = "2024-2025"

# Create folders
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['SCREENSHOT_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)

# Load credentials from JSON file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
print(f"DEBUG: Looking for credentials file at: {CREDENTIALS_FILE}")  # Debug
try:
    with open(CREDENTIALS_FILE, 'r') as f:
        CREDENTIALS = json.load(f)
    print(f"DEBUG: Loaded credentials: {[cred['username'] for cred in CREDENTIALS]}")  # Debug
except FileNotFoundError:
    print(f"ERROR: Credentials file not found at {CREDENTIALS_FILE}")
    CREDENTIALS = []
except json.JSONDecodeError:
    print(f"ERROR: Invalid JSON in {CREDENTIALS_FILE}")
    CREDENTIALS = []

# Global variables for processing state
processing_state = {
    'is_processing': False,
    'progress': 0,
    'logs': [],
    'successful_count': 0,
    'total_records': 0,
    'current_aadhaar': ''
}

# Setup logging
logging.basicConfig(filename=app.config['LOG_FILE'], level=logging.INFO, format='%(asctime)s: %(message)s')

def log_message(message, tag="info"):
    formatted_message = f"{message}"
    processing_state['logs'].append({'message': formatted_message, 'tag': tag})
    logging.info(f"[{tag}] {message}")
    print(f"LOG: [{tag}] {message}")  # Debug

# Automation Helper Functions
def slow_typing(element, text, delay_range=(0.1, 0.3)):
    """Type text with human-like delay between characters"""
    for char in text:
        element.type(char)
        time.sleep(random.uniform(*delay_range))

def slow_action(description, action_func, delay=2.5, max_delay=4.0):
    """Execute an action with visual feedback and random delay"""
    log_message(f"⏳ {description}...", "info")
    time.sleep(random.uniform(delay, max_delay))
    try:
        result = action_func()
        log_message(f"✅ {description} completed", "success")
        time.sleep(1)
        return True, result
    except Exception as e:
        log_message(f"❌ Failed: {description} - {str(e)}", "error")
        return False, None

def handle_popups(page):
    """Handle any popup dialogs"""
    try:
        # Try multiple ways to find OK buttons
        selectors = [
            "button:has-text('OK')",
            "button.btn-primary:has-text('OK')",
            "button.modal-btn:has-text('OK')",
            ".modal-footer button:has-text('OK')",
            "[role='button']:has-text('OK')"
        ]
        
        for selector in selectors:
            try:
                ok_button = page.locator(selector).first
                if ok_button.is_visible(timeout=1000):
                    ok_button.click(timeout=3000)
                    log_message("⚠️ Popup handled with selector: " + selector, "info")
                    time.sleep(1)
                    return True
            except:
                continue
        
        # Also try by role as fallback
        try:
            role_button = page.get_by_role("button", name="OK").first
            if role_button.is_visible(timeout=1000):
                role_button.click(timeout=3000)
                log_message("⚠️ Popup handled with role selector", "info")
                time.sleep(1)
                return True
        except:
            pass
            
        return False
    except:
        return False

def manual_login(page):
    """Handle login with manual CAPTCHA entry"""
    log_message("\n=== MANUAL LOGIN ===", "info")
    
    def fill_credentials():
        mobile_field = page.get_by_role("textbox", name="Enter Your Mobile No.")
        mobile_field.click()
        slow_typing(mobile_field, CREDENTIALS[0]['username'])
        
        password_field = page.get_by_role("textbox", name="Password")
        password_field.click()
        slow_typing(password_field, CREDENTIALS[0]['password'])
    
    success, _ = slow_action("Entering credentials", fill_credentials)
    if not success:
        return False

    log_message("🧩 PLEASE ENTER CAPTCHA MANUALLY AND CLICK LOGIN...", "info")
    start_time = time.time()
    while True:
        try:
            page.wait_for_selector("span:has-text('Welcome')", timeout=5000)
            break
        except:
            elapsed = int(time.time() - start_time)
            log_message(f"⏳ Waiting... {elapsed}s", "info")
            if elapsed > 120:
                raise TimeoutError("CAPTCHA timeout")
    log_message("\n✅ Login successful!", "success")
    time.sleep(2)
    handle_popups(page)
    return True

def select_account_number(page):
    """Select account number from dropdown with specific HTML structure"""
    try:
        # Wait for dropdown to be visible
        page.wait_for_selector("select[name='accountNumbers']", timeout=15000)
        
        # Get the dropdown element
        account_dropdown = page.locator("select[name='accountNumbers']")
        
        # Get all options
        options = account_dropdown.locator("option").all()
        
        if len(options) < 2:
            raise Exception("No account options found in dropdown")
        
        # Select the first actual account (skip the disabled option)
        first_account = options[1].get_attribute("value")
        account_dropdown.select_option(first_account)
        
        log_message(f"✅ Successfully selected account: {first_account}", "success")
        return True
        
    except Exception as e:
        log_message(f"❌ Failed to select account: {str(e)}", "error")
        # Take screenshot for debugging
        timestamp = datetime.now().strftime("%H%M%S")
        page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/account_selection_error_{timestamp}.png")
        return False

def try_different_ok_clicks(page):
    """Try multiple ways to click the OK button"""
    methods = [
        lambda: page.get_by_role("button", name="OK").click(timeout=5000),
        lambda: page.locator("button:has-text('OK')").click(timeout=5000),
        lambda: page.locator(".modal-footer button:has-text('OK')").click(timeout=5000),
        lambda: page.locator("button.btn-primary:has-text('OK')").click(timeout=5000),
        lambda: page.locator("[role='button']:has-text('OK')").click(timeout=5000),
        lambda: page.keyboard.press("Enter", timeout=5000)  # Sometimes Enter key works when buttons don't
    ]
    
    for i, method in enumerate(methods):
        try:
            log_message(f"Trying OK click method {i+1}...", "info")
            method()
            log_message(f"✅ OK click method {i+1} worked!", "success")
            time.sleep(2)  # Wait to see the effect
            return True
        except Exception as e:
            log_message(f"Method {i+1} failed: {e}", "error")
            pass
    
    log_message("❌ All OK click methods failed", "error")
    return False

def process_single_application(page, aadhaar):
    """Process a single Aadhaar application"""
    try:
        processing_state['current_aadhaar'] = aadhaar
        
        # Navigate to loan page
        success, _ = slow_action("Opening loan page", 
                               lambda: page.get_by_role("link", name="Loan Application Loan").click(),
                               3, 4)
        if not success:
            raise Exception("Could not open loan page")
        
        # Select financial year
        success, _ = slow_action("Selecting financial year",
                               lambda: page.get_by_role("combobox").select_option(FINANCIAL_YEAR))
        if not success:
            raise Exception("Financial year selection failed")
        
        # Enter Aadhaar
        def fill_aadhaar():
            field = page.get_by_role("textbox", name="Enter Aadhaar No.")
            field.click()
            field.fill("")  # Clear the field first
            slow_typing(field, aadhaar)
            
        success, _ = slow_action("Entering Aadhaar", fill_aadhaar)
        if not success:
            raise Exception("Aadhaar entry failed")
        
        # Fetch record
        success, _ = slow_action("Clicking FETCH RECORD",
                               lambda: page.get_by_role("button", name="FETCH RECORD").click(),
                               3, 4)
        if not success:
            raise Exception("Fetch record failed")
        
        # Check for No Records Found or other error messages
        try:
            error_message = page.locator("div.toast-message").text_content(timeout=5000)
            if "No Records Found" in error_message or "error" in error_message.lower():
                log_message(f"⚠️ Error for Aadhaar {aadhaar}: {error_message}", "warning")
                # Take screenshot of the error
                timestamp = datetime.now().strftime("%H%M%S")
                page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/no_record_{aadhaar}_{timestamp}.png")
                return False  # Skip to next Aadhaar
        except:
            pass  # No error message found, continue
        
        # Select account number
        if not select_account_number(page):
            raise Exception("Account selection failed")
        
        # Click OK button (using enhanced method)
        success = try_different_ok_clicks(page)
        if not success:
            # Take screenshot for debugging
            timestamp = datetime.now().strftime("%H%M%S")
            page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/ok_button_error_{aadhaar}_{timestamp}.png")
            raise Exception("OK button click failed despite multiple attempts")

        # Take a screenshot at this point to aid debugging
        timestamp = datetime.now().strftime("%H%M%S")
        page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/after_ok_click_{aadhaar}_{timestamp}.png")
        time.sleep(2)  # Extra wait to ensure page loads properly

        # Continue with the loan application process
        steps = [
            ("Selecting application type", 
             lambda: page.locator("select[name='applicationType']").select_option("0")),
            ("Clicking page content", 
             lambda: page.locator(".pageMainContent").click()),
            ("UPDATE & CONTINUE (1st time)", 
             lambda: page.get_by_role("button", name="UPDATE & CONTINUE").click()),
            ("UPDATE & CONTINUE (2nd time)", 
             lambda: page.get_by_role("button", name="UPDATE & CONTINUE").click()),
            ("Completing financial details (step 1)", 
             lambda: page.get_by_role("tabpanel", name="Financial Details").get_by_role("img").click()),
            ("Completing financial details (step 2)", 
             lambda: page.get_by_role("tabpanel", name="Financial Details").locator("i").nth(1).click()),
            ("Selecting financial option", 
             lambda: page.get_by_label("Financial Details").get_by_text("1", exact=True).click()),
            ("SAVE & CONTINUE (1st time)", 
             lambda: page.get_by_role("button", name="SAVE & CONTINUE").click()),
            ("SAVE & CONTINUE (2nd time)", 
             lambda: page.get_by_role("button", name="SAVE & CONTINUE").click()),
            ("Clicking Preview", 
             lambda: page.get_by_role("button", name="Preview").click()),
            ("Clicking SUBMIT", 
             lambda: page.get_by_role("button", name="SUBMIT").click()),
            ("Clicking CONFIRM", 
             lambda: page.get_by_role("button", name="CONFIRM").click())
        ]

        for desc, action in steps:
            success, _ = slow_action(desc, action)
            if not success:
                raise Exception(f"Process failed at: {desc}")
            
            handle_popups(page)
            time.sleep(1)
        
        # Final OK click with enhanced method
        log_message("Attempting final OK click...", "info")
        success = try_different_ok_clicks(page)
        if not success:
            # Try a different approach as last resort
            log_message("Trying alternative click methods for final OK...", "info")
            try:
                # JavaScript click as last resort
                page.evaluate("() => { document.querySelector('button:has-text(\"OK\")').click(); }")
                log_message("✅ JavaScript click worked for final OK!", "success")
                success = True
            except:
                # Take screenshot for debugging
                timestamp = datetime.now().strftime("%H%M%S_%f")
                page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/final_ok_error_{aadhaar}_{timestamp}.png")
                log_message("❌ All methods to click final OK failed", "error")
        
        log_message(f"🎉 Application completed for Aadhaar: {aadhaar}", "success")
        return True

    except Exception as e:
        log_message(f"❌ Processing failed for {aadhaar}: {e}", "error")
        timestamp = datetime.now().strftime("%H%M%S_%f")
        screenshot_path = os.path.join(app.config['SCREENSHOT_FOLDER'], f"error_{aadhaar}_{timestamp}.png")
        page.screenshot(path=screenshot_path, full_page=True)
        log_message(f"📸 Screenshot saved to: {screenshot_path}", "info")
        return False

# Routes
@app.route('/')
def login():
    if 'logged_in' in session:
        print(f"DEBUG: User is logged in, redirecting to upload. Session: {session}")  # Debug
        return redirect(url_for('upload'))
    print("DEBUG: User not logged in, rendering login page")  # Debug
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def do_login():
    username = request.form['username']
    password = request.form['password']
    print(f"DEBUG: Attempting login with username: {username}")  # Debug
    for cred in CREDENTIALS:
        if username == cred['username'] and password == cred['password']:
            session['logged_in'] = True
            print(f"LOGIN: Successful login for {username}. Session: {session}")  # Debug
            return redirect(url_for('upload'))
    flash('Invalid username or password', 'error')
    print(f"LOGIN ERROR: Invalid credentials for {username}")  # Debug
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('temp_records_file', None)
    session.pop('file_path', None)
    print(f"LOGOUT: Session cleared. Session: {session}")  # Debug
    return redirect(url_for('login'))

@app.route('/home')
def home():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    # You'll need to get these from your session or database
    username = "User"  # Replace with actual username
    mobile = "9876543210"  # Replace with actual mobile number
    
    return render_template('home.html', username=username, mobile=mobile)    

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'logged_in' not in session:
        print(f"UPLOAD: Redirecting to login - not logged in. Session: {session}")  # Debug
        return redirect(url_for('login'))
    
     # Get user details
    username = "User"  # Replace with actual username
    mobile = "9876543210"  # Replace with actual mobile number
    
    # Rest of your existing upload function...
  

    if request.method == 'POST':
        print("UPLOAD: Received POST request")  # Debug
        if 'file' not in request.files:
            flash('No file selected', 'error')
            print("UPLOAD ERROR: No file selected")  # Debug
            return redirect(url_for('upload'))
        file = request.files['file']
        if file.filename == '':
            flash('No file selected', 'error')
            print("UPLOAD ERROR: Empty filename")  # Debug
            return redirect(url_for('upload'))
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            print(f"UPLOAD: File saved to {file_path}")  # Debug
            
            try:
                print("UPLOAD: Reading Excel file (first 3 rows for validation)")  # Debug
                df = pd.read_excel(file_path, sheet_name=SHEET_NAME, nrows=3)
                print(f"UPLOAD: Excel columns: {list(df.columns)}")  # Debug
                if 'Aadhar No' not in df.columns:
                    flash("Excel must contain 'Aadhar No' column", 'error')
                    print("UPLOAD ERROR: 'Aadhar No' column missing")  # Debug
                    return redirect(url_for('upload'))
                
                # Read full file for processing
                print("UPLOAD: Reading full Excel file")  # Debug
                df = pd.read_excel(file_path, sheet_name=SHEET_NAME)
                print(f"UPLOAD: Raw 'Aadhar No' values (first 5): {df['Aadhar No'].head().tolist()}")  # Debug
                
                # Clean Aadhaar numbers
                def clean_aadhar(x):
                    if pd.isna(x):
                        return ''
                    x = str(x)
                    x = x.split('.')[0]
                    x = re.sub(r'[^0-9]', '', x)
                    if len(x) > 12:
                        x = x[:12]
                    elif len(x) < 12:
                        x = x.zfill(12)
                    return x
                
                df['Aadhar No'] = df['Aadhar No'].apply(clean_aadhar)
                print(f"UPLOAD: Processed 'Aadhar No' values (first 5): {df['Aadhar No'].head().tolist()}")  # Debug
                
                print(f"UPLOAD: Total rows after cleaning: {len(df)}")  # Debug
                df = df[df['Aadhar No'].notna() & (df['Aadhar No'] != '')]
                print(f"UPLOAD: Rows after removing NA/empty: {len(df)}")  # Debug
                
                # Validate Aadhaar numbers
                invalid_aadhaars = []
                for idx, aadhaar in df['Aadhar No'].items():
                    if not re.match(r'^\d{12}$', aadhaar):
                        invalid_aadhaars.append((idx, aadhaar, f"Invalid format: {aadhaar}"))
                
                if invalid_aadhaars:
                    print("UPLOAD: Invalid Aadhaar numbers found:")
                    for idx, aadhaar, reason in invalid_aadhaars[:5]:
                        print(f"  Row {idx}: {aadhaar} - {reason}")  # Debug
                    if len(invalid_aadhaars) == len(df):
                        flash(f"No valid Aadhaar numbers found. Issues: {invalid_aadhaars[0][2]} (and {len(invalid_aadhaars)-1} more)", 'error')
                        print("UPLOAD ERROR: No valid Aadhaar numbers")  # Debug
                        return redirect(url_for('upload'))
                
                df = df[df['Aadhar No'].str.match(r'^\d{12}$')]
                print(f"UPLOAD: Valid Aadhaar rows: {len(df)}")  # Debug
                
                if len(df) == 0:
                    flash("No valid Aadhaar numbers found after validation", 'error')
                    print("UPLOAD ERROR: No valid Aadhaar numbers after validation")  # Debug
                    return redirect(url_for('upload'))
                
                # Save records to temporary JSON file
                records = df.to_dict('records')
                temp_filename = f"records_{uuid.uuid4().hex}.json"
                temp_filepath = os.path.join(app.config['TEMP_FOLDER'], temp_filename)
                with open(temp_filepath, 'w') as f:
                    json.dump(records, f)
                print(f"UPLOAD: Records saved to temporary file: {temp_filepath}")  # Debug
                
                # Store file paths in session
                session['temp_records_file'] = temp_filepath
                session['file_path'] = file_path
                print(f"UPLOAD: Session updated with temp_records_file: {temp_filepath}, file_path: {file_path}. Session: {session}")  # Debug
                
                preview = "\n".join(str(r['Aadhar No']) for r in records)
                log_message(f"Loaded {len(records)} valid Aadhaar numbers", "success")
                print(f"UPLOAD: Preview generated, {len(records)} records stored in temp file")  # Debug
                return render_template('upload.html', preview=preview, file_uploaded=True, total_records=len(records))
            except Exception as e:
                flash(f"Failed to load Excel: {str(e)}", 'error')
                log_message(f"Failed to load Excel: {str(e)}", "error")
                print(f"UPLOAD ERROR: Exception - {str(e)}")  # Debug
                return redirect(url_for('upload'))
    
    print(f"UPLOAD: Rendering upload page (GET request). Session: {session}")  # Debug
    from flask import Flask, request, render_template, redirect, url_for, jsonify, flash, session
from werkzeug.utils import secure_filename
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError
import time
import pandas as pd
import os
import threading
import logging
import re
import json
import uuid
import random

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Required for session and flash messages
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'Uploads')
app.config['SCREENSHOT_FOLDER'] = os.path.join(os.getcwd(), 'screenshots')
app.config['TEMP_FOLDER'] = os.path.join(os.getcwd(), 'temp')  # New folder for temp files
app.config['LOG_FILE'] = os.path.join(os.getcwd(), 'process.log')

# Configuration
SHEET_NAME = "Sheet1"
FINANCIAL_YEAR = "2024-2025"

# Create folders
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['SCREENSHOT_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)

# Load credentials from JSON file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
print(f"DEBUG: Looking for credentials file at: {CREDENTIALS_FILE}")  # Debug
try:
    with open(CREDENTIALS_FILE, 'r') as f:
        CREDENTIALS = json.load(f)
    print(f"DEBUG: Loaded credentials: {[cred['username'] for cred in CREDENTIALS]}")  # Debug
except FileNotFoundError:
    print(f"ERROR: Credentials file not found at {CREDENTIALS_FILE}")
    CREDENTIALS = []
except json.JSONDecodeError:
    print(f"ERROR: Invalid JSON in {CREDENTIALS_FILE}")
    CREDENTIALS = []

# Global variables for processing state
processing_state = {
    'is_processing': False,
    'progress': 0,
    'logs': [],
    'successful_count': 0,
    'total_records': 0,
    'current_aadhaar': ''
}

# Setup logging
logging.basicConfig(filename=app.config['LOG_FILE'], level=logging.INFO, format='%(asctime)s: %(message)s')

def log_message(message, tag="info"):
    formatted_message = f"{message}"
    processing_state['logs'].append({'message': formatted_message, 'tag': tag})
    logging.info(f"[{tag}] {message}")
    print(f"LOG: [{tag}] {message}")  # Debug

# Automation Helper Functions
def slow_typing(element, text, delay_range=(0.1, 0.3)):
    """Type text with human-like delay between characters"""
    for char in text:
        element.type(char)
        time.sleep(random.uniform(*delay_range))

def slow_action(description, action_func, delay=2.5, max_delay=4.0):
    """Execute an action with visual feedback and random delay"""
    log_message(f"⏳ {description}...", "info")
    time.sleep(random.uniform(delay, max_delay))
    try:
        result = action_func()
        log_message(f"✅ {description} completed", "success")
        time.sleep(1)
        return True, result
    except Exception as e:
        log_message(f"❌ Failed: {description} - {str(e)}", "error")
        return False, None

def handle_popups(page):
    """Handle any popup dialogs"""
    try:
        # Try multiple ways to find OK buttons
        selectors = [
            "button:has-text('OK')",
            "button.btn-primary:has-text('OK')",
            "button.modal-btn:has-text('OK')",
            ".modal-footer button:has-text('OK')",
            "[role='button']:has-text('OK')"
        ]
        
        for selector in selectors:
            try:
                ok_button = page.locator(selector).first
                if ok_button.is_visible(timeout=1000):
                    ok_button.click(timeout=3000)
                    log_message("⚠️ Popup handled with selector: " + selector, "info")
                    time.sleep(1)
                    return True
            except:
                continue
        
        # Also try by role as fallback
        try:
            role_button = page.get_by_role("button", name="OK").first
            if role_button.is_visible(timeout=1000):
                role_button.click(timeout=3000)
                log_message("⚠️ Popup handled with role selector", "info")
                time.sleep(1)
                return True
        except:
            pass
            
        return False
    except:
        return False

def manual_login(page):
    """Handle login with manual CAPTCHA entry"""
    log_message("\n=== MANUAL LOGIN ===", "info")
    
    def fill_credentials():
        mobile_field = page.get_by_role("textbox", name="Enter Your Mobile No.")
        mobile_field.click()
        slow_typing(mobile_field, CREDENTIALS[0]['username'])
        
        password_field = page.get_by_role("textbox", name="Password")
        password_field.click()
        slow_typing(password_field, CREDENTIALS[0]['password'])
    
    success, _ = slow_action("Entering credentials", fill_credentials)
    if not success:
        return False

    log_message("🧩 PLEASE ENTER CAPTCHA MANUALLY AND CLICK LOGIN...", "info")
    start_time = time.time()
    while True:
        try:
            page.wait_for_selector("span:has-text('Welcome')", timeout=5000)
            break
        except:
            elapsed = int(time.time() - start_time)
            log_message(f"⏳ Waiting... {elapsed}s", "info")
            if elapsed > 120:
                raise TimeoutError("CAPTCHA timeout")
    log_message("\n✅ Login successful!", "success")
    time.sleep(2)
    handle_popups(page)
    return True

def select_account_number(page):
    """Select account number from dropdown with specific HTML structure"""
    try:
        # Wait for dropdown to be visible
        page.wait_for_selector("select[name='accountNumbers']", timeout=15000)
        
        # Get the dropdown element
        account_dropdown = page.locator("select[name='accountNumbers']")
        
        # Get all options
        options = account_dropdown.locator("option").all()
        
        if len(options) < 2:
            raise Exception("No account options found in dropdown")
        
        # Select the first actual account (skip the disabled option)
        first_account = options[1].get_attribute("value")
        account_dropdown.select_option(first_account)
        
        log_message(f"✅ Successfully selected account: {first_account}", "success")
        return True
        
    except Exception as e:
        log_message(f"❌ Failed to select account: {str(e)}", "error")
        # Take screenshot for debugging
        timestamp = datetime.now().strftime("%H%M%S")
        page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/account_selection_error_{timestamp}.png")
        return False

def try_different_ok_clicks(page):
    """Try multiple ways to click the OK button"""
    methods = [
        lambda: page.get_by_role("button", name="OK").click(timeout=5000),
        lambda: page.locator("button:has-text('OK')").click(timeout=5000),
        lambda: page.locator(".modal-footer button:has-text('OK')").click(timeout=5000),
        lambda: page.locator("button.btn-primary:has-text('OK')").click(timeout=5000),
        lambda: page.locator("[role='button']:has-text('OK')").click(timeout=5000),
        lambda: page.keyboard.press("Enter", timeout=5000)  # Sometimes Enter key works when buttons don't
    ]
    
    for i, method in enumerate(methods):
        try:
            log_message(f"Trying OK click method {i+1}...", "info")
            method()
            log_message(f"✅ OK click method {i+1} worked!", "success")
            time.sleep(2)  # Wait to see the effect
            return True
        except Exception as e:
            log_message(f"Method {i+1} failed: {e}", "error")
            pass
    
    log_message("❌ All OK click methods failed", "error")
    return False

def process_single_application(page, aadhaar):
    """Process a single Aadhaar application"""
    try:
        processing_state['current_aadhaar'] = aadhaar
        
        # Navigate to loan page
        success, _ = slow_action("Opening loan page", 
                               lambda: page.get_by_role("link", name="Loan Application Loan").click(),
                               3, 4)
        if not success:
            raise Exception("Could not open loan page")
        
        # Select financial year
        success, _ = slow_action("Selecting financial year",
                               lambda: page.get_by_role("combobox").select_option(FINANCIAL_YEAR))
        if not success:
            raise Exception("Financial year selection failed")
        
        # Enter Aadhaar
        def fill_aadhaar():
            field = page.get_by_role("textbox", name="Enter Aadhaar No.")
            field.click()
            field.fill("")  # Clear the field first
            slow_typing(field, aadhaar)
            
        success, _ = slow_action("Entering Aadhaar", fill_aadhaar)
        if not success:
            raise Exception("Aadhaar entry failed")
        
        # Fetch record
        success, _ = slow_action("Clicking FETCH RECORD",
                               lambda: page.get_by_role("button", name="FETCH RECORD").click(),
                               3, 4)
        if not success:
            raise Exception("Fetch record failed")
        
        # Check for No Records Found or other error messages
        try:
            error_message = page.locator("div.toast-message").text_content(timeout=5000)
            if "No Records Found" in error_message or "error" in error_message.lower():
                log_message(f"⚠️ Error for Aadhaar {aadhaar}: {error_message}", "warning")
                # Take screenshot of the error
                timestamp = datetime.now().strftime("%H%M%S")
                page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/no_record_{aadhaar}_{timestamp}.png")
                return False  # Skip to next Aadhaar
        except:
            pass  # No error message found, continue
        
        # Select account number
        if not select_account_number(page):
            raise Exception("Account selection failed")
        
        # Click OK button (using enhanced method)
        success = try_different_ok_clicks(page)
        if not success:
            # Take screenshot for debugging
            timestamp = datetime.now().strftime("%H%M%S")
            page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/ok_button_error_{aadhaar}_{timestamp}.png")
            raise Exception("OK button click failed despite multiple attempts")

        # Take a screenshot at this point to aid debugging
        timestamp = datetime.now().strftime("%H%M%S")
        page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/after_ok_click_{aadhaar}_{timestamp}.png")
        time.sleep(2)  # Extra wait to ensure page loads properly

        # Continue with the loan application process
        steps = [
            ("Selecting application type", 
             lambda: page.locator("select[name='applicationType']").select_option("0")),
            ("Clicking page content", 
             lambda: page.locator(".pageMainContent").click()),
            ("UPDATE & CONTINUE (1st time)", 
             lambda: page.get_by_role("button", name="UPDATE & CONTINUE").click()),
            ("UPDATE & CONTINUE (2nd time)", 
             lambda: page.get_by_role("button", name="UPDATE & CONTINUE").click()),
            ("Completing financial details (step 1)", 
             lambda: page.get_by_role("tabpanel", name="Financial Details").get_by_role("img").click()),
            ("Completing financial details (step 2)", 
             lambda: page.get_by_role("tabpanel", name="Financial Details").locator("i").nth(1).click()),
            ("Selecting financial option", 
             lambda: page.get_by_label("Financial Details").get_by_text("1", exact=True).click()),
            ("SAVE & CONTINUE (1st time)", 
             lambda: page.get_by_role("button", name="SAVE & CONTINUE").click()),
            ("SAVE & CONTINUE (2nd time)", 
             lambda: page.get_by_role("button", name="SAVE & CONTINUE").click()),
            ("Clicking Preview", 
             lambda: page.get_by_role("button", name="Preview").click()),
            ("Clicking SUBMIT", 
             lambda: page.get_by_role("button", name="SUBMIT").click()),
            ("Clicking CONFIRM", 
             lambda: page.get_by_role("button", name="CONFIRM").click())
        ]

        for desc, action in steps:
            success, _ = slow_action(desc, action)
            if not success:
                raise Exception(f"Process failed at: {desc}")
            
            handle_popups(page)
            time.sleep(1)
        
        # Final OK click with enhanced method
        log_message("Attempting final OK click...", "info")
        success = try_different_ok_clicks(page)
        if not success:
            # Try a different approach as last resort
            log_message("Trying alternative click methods for final OK...", "info")
            try:
                # JavaScript click as last resort
                page.evaluate("() => { document.querySelector('button:has-text(\"OK\")').click(); }")
                log_message("✅ JavaScript click worked for final OK!", "success")
                success = True
            except:
                # Take screenshot for debugging
                timestamp = datetime.now().strftime("%H%M%S_%f")
                page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/final_ok_error_{aadhaar}_{timestamp}.png")
                log_message("❌ All methods to click final OK failed", "error")
        
        log_message(f"🎉 Application completed for Aadhaar: {aadhaar}", "success")
        return True

    except Exception as e:
        log_message(f"❌ Processing failed for {aadhaar}: {e}", "error")
        timestamp = datetime.now().strftime("%H%M%S_%f")
        screenshot_path = os.path.join(app.config['SCREENSHOT_FOLDER'], f"error_{aadhaar}_{timestamp}.png")
        page.screenshot(path=screenshot_path, full_page=True)
        log_message(f"📸 Screenshot saved to: {screenshot_path}", "info")
        return False

# Routes
@app.route('/')
def login():
    if 'logged_in' in session:
        print(f"DEBUG: User is logged in, redirecting to upload. Session: {session}")  # Debug
        return redirect(url_for('upload'))
    print("DEBUG: User not logged in, rendering login page")  # Debug
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def do_login():
    username = request.form['username']
    password = request.form['password']
    print(f"DEBUG: Attempting login with username: {username}")  # Debug
    for cred in CREDENTIALS:
        if username == cred['username'] and password == cred['password']:
            session['logged_in'] = True
            session['username'] = username  # Store username in session
            session['mobile'] = cred.get('mobile', '9876543210')  # Store mobile if available
            print(f"LOGIN: Successful login for {username}. Session: {session}")  # Debug
            return redirect(url_for('home'))  # Changed to redirect to home
    flash('Invalid username or password', 'error')
    return redirect(url_for('login'))


# Add these new routes
@app.route('/download')
def download_file():
    file_path = request.args.get('file')
    if not file_path or not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)



# Update the processing function to return output file
def run_processing(records):
    try:
        output_filename = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
        
        # ... existing processing code ...
        
        # After processing completes
        output_df = pd.DataFrame([{
            'Aadhar No': r['Aadhar No'],
            'Status': 'Success' if r['Aadhar No'] in successful_aadhaars else 'Failure'
        } for r in records])
        
        output_df.to_excel(output_path, index=False)
        
        processing_state['output_file'] = output_path
        processing_state['is_processing'] = False
        
    except Exception as e:
        log_message(f"Fatal error: {str(e)}", "error")
        processing_state['is_processing'] = False

# # Update logs endpoint to include output file
# @app.route('/logs')
# def get_logs():
#     return jsonify({
#         'logs': processing_state['logs'],
#         'progress': processing_state['progress'],
#         'is_processing': processing_state['is_processing'],
#         'successful_count': processing_state['successful_count'],
#         'total_records': processing_state['total_records'],
#         'output_file': processing_state.get('output_file', ''),
#         'current_aadhaar': processing_state['current_aadhaar']
#     })
@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('temp_records_file', None)
    session.pop('file_path', None)
    print(f"LOGOUT: Session cleared. Session: {session}")  # Debug
    return redirect(url_for('login'))

@app.route('/home')
def home():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    mobile = session.get('mobile', '9876543210')
    
    return render_template('home.html', username=username, mobile=mobile)

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'logged_in' not in session:
        print(f"UPLOAD: Redirecting to login - not logged in. Session: {session}")  # Debug
        return redirect(url_for('login'))
    
    # Get user details
    username = "User"  # Replace with actual username
    mobile = "9876543210"  # Replace with actual mobile number
    
    file_uploaded = False
    preview = ""
    total_records = 0

    if request.method == 'POST':
        print("UPLOAD: Received POST request")  # Debug
        if 'file' not in request.files:
            flash('No file selected', 'error')
            print("UPLOAD ERROR: No file selected")  # Debug
            return redirect(url_for('upload'))
        file = request.files['file']
        if file.filename == '':
            flash('No file selected', 'error')
            print("UPLOAD ERROR: Empty filename")  # Debug
            return redirect(url_for('upload'))
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            print(f"UPLOAD: File saved to {file_path}")  # Debug
            
            try:
                print("UPLOAD: Reading Excel file (first 3 rows for validation)")  # Debug
                df = pd.read_excel(file_path, sheet_name=SHEET_NAME, nrows=3)
                print(f"UPLOAD: Excel columns: {list(df.columns)}")  # Debug
                if 'Aadhar No' not in df.columns:
                    flash("Excel must contain 'Aadhar No' column", 'error')
                    print("UPLOAD ERROR: 'Aadhar No' column missing")  # Debug
                    return redirect(url_for('upload'))
                
                # Read full file for processing
                print("UPLOAD: Reading full Excel file")  # Debug
                df = pd.read_excel(file_path, sheet_name=SHEET_NAME)
                
                # Clean Aadhaar numbers
                def clean_aadhar(x):
                    if pd.isna(x):
                        return ''
                    x = str(x)
                    x = x.split('.')[0]
                    x = re.sub(r'[^0-9]', '', x)
                    if len(x) > 12:
                        x = x[:12]
                    elif len(x) < 12:
                        x = x.zfill(12)
                    return x
                
                df['Aadhar No'] = df['Aadhar No'].apply(clean_aadhar)
                df = df[df['Aadhar No'].notna() & (df['Aadhar No'] != '')]
                df = df[df['Aadhar No'].str.match(r'^\d{12}$')]
                
                if len(df) == 0:
                    flash("No valid Aadhaar numbers found after validation", 'error')
                    return redirect(url_for('upload'))
                
                # Save records to temporary JSON file
                records = df.to_dict('records')
                temp_filename = f"records_{uuid.uuid4().hex}.json"
                temp_filepath = os.path.join(app.config['TEMP_FOLDER'], temp_filename)
                with open(temp_filepath, 'w') as f:
                    json.dump(records, f)
                
                # Store file paths in session
                session['temp_records_file'] = temp_filepath
                session['file_path'] = file_path
                
                preview = "\n".join(str(r['Aadhar No']) for r in records[:10])  # Show first 10 only
                file_uploaded = True
                total_records = len(records)
                
                return render_template('upload.html', 
                         username=username, 
                         mobile=mobile,
                         preview=preview,
                         file_uploaded=file_uploaded,
                         total_records=total_records)
                
            except Exception as e:
                flash(f"Failed to load Excel: {str(e)}", 'error')
                return redirect(url_for('upload'))
    
    return render_template('upload.html', 
                         username=username, 
                         mobile=mobile,
                         preview=preview,
                         file_uploaded=file_uploaded,
                         total_records=total_records)


@app.route('/start_processing', methods=['POST'])
def start_processing():
    print(f"DEBUG: Start processing called. Session: {session}")  # Debug
    if 'logged_in' not in session or 'temp_records_file' not in session:
        flash('Please upload a valid file first', 'error')
        print(f"START_PROCESSING ERROR: No records or not logged in. Session: {session}")  # Debug
        return redirect(url_for('upload'))
    
    if processing_state['is_processing']:
        flash('Processing already in progress', 'error')
        print("START_PROCESSING ERROR: Processing already in progress")  # Debug
        return redirect(url_for('upload'))
    
    # Load records from temporary file
    temp_filepath = session['temp_records_file']
    print(f"DEBUG: Loading records from temp file: {temp_filepath}")  # Debug
    try:
        with open(temp_filepath, 'r') as f:
            records = json.load(f)
        print(f"DEBUG: Loaded {len(records)} records from temp file")  # Debug
    except Exception as e:
        flash(f"Failed to load records: {str(e)}", 'error')
        print(f"START_PROCESSING ERROR: Failed to load records from {temp_filepath} - {str(e)}")  # Debug
        return redirect(url_for('upload'))
    
    processing_state['is_processing'] = True
    processing_state['progress'] = 0
    processing_state['logs'] = []
    processing_state['successful_count'] = 0
    processing_state['total_records'] = len(records)
    processing_state['current_aadhaar'] = ''
    
    print(f"START_PROCESSING: Starting thread with {len(records)} records")  # Debug
    threading.Thread(target=run_processing, args=(records,), daemon=True).start()
    return redirect(url_for('upload'))

@app.route('/logs')
def get_logs():
    print(f"DEBUG: Fetching logs. Processing state: {processing_state}")  # Debug
    return jsonify({
        'logs': processing_state['logs'],
        'progress': processing_state['progress'],
        'is_processing': processing_state['is_processing'],
        'successful_count': processing_state['successful_count'],
        'total_records': processing_state['total_records'],
        'current_aadhaar': processing_state['current_aadhaar']
    })

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'xlsx', 'xls'}

def run_processing(records):
    try:
        log_message("Starting Playwright processing", "info")
        # Track Aadhaar status for Excel output
        aadhaar_status = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=0, args=["--start-maximized"])
            context = browser.new_context(viewport={"width": 1366, "height": 768}, ignore_https_errors=True)
            page = context.new_page()

            log_message("Opening website...", "info")
            print("DEBUG: Navigating to https://fasalrin.gov.in/login")  # Debug
            page.goto('https://fasalrin.gov.in/login', timeout=60000)
            time.sleep(3)

            print("DEBUG: Waiting for manual login")  # Debug
            if not manual_login(page):
                raise Exception("Login failed")

            successful_count = 0
            for i, record in enumerate(records):
                aadhaar = record['Aadhar No']
                processing_state['current_aadhaar'] = aadhaar
                log_message(f"Processing {aadhaar} ({i+1}/{len(records)})", "info")
                print(f"DEBUG: Processing Aadhaar {aadhaar} ({i+1}/{len(records)})")  # Debug
                processing_state['progress'] = ((i + 1) / len(records)) * 100

                try:
                    if i > 0:
                        print("DEBUG: Navigating to dashboard")  # Debug
                        page.goto('https://fasalrin.gov.in/dashboard')
                        time.sleep(3)

                    success = process_single_application(page, aadhaar)
                    status = "Success" if success else "Failure"
                    aadhaar_status.append({"Aadhar No": aadhaar, "Status": status})
                    if success:
                        successful_count += 1
                        processing_state['successful_count'] = successful_count
                        log_message(f"Success: {aadhaar}", "success")
                        print(f"DEBUG: Success for Aadhaar {aadhaar}")  # Debug
                    else:
                        log_message(f"Failed: {aadhaar}", "error")
                        print(f"DEBUG: Failed for Aadhaar {aadhaar}")  # Debug
                    time.sleep(2)
                except Exception as e:
                    log_message(f"Error: {aadhaar} - {str(e)}", "error")
                    print(f"DEBUG: Error for Aadhaar {aadhaar}: {str(e)}")  # Debug
                    page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/error_{aadhaar}.png")
            
            # Save Aadhaar status to Excel
            output_df = pd.DataFrame(aadhaar_status)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], f"processing_results_{timestamp}.xlsx")
            output_df.to_excel(output_path, index=False)
            log_message(f"📊 Excel output saved to: {output_path}", "success")
            print(f"DEBUG: Excel output saved to: {output_path}")  # Debug
            
            log_message(f"Completed: Processed {successful_count}/{len(records)} Aadhaar numbers", "success")
            print(f"DEBUG: Processing completed. {successful_count}/{len(records)} successful")  # Debug
            processing_state['is_processing'] = False
            browser.close()
    except Exception as e:
        log_message(f"Fatal error: {str(e)}", "error")
        print(f"DEBUG: Fatal error in processing: {str(e)}")  # Debug
        processing_state['is_processing'] = False

if __name__ == '__main__':
    print("DEBUG: Starting Flask app")  # Debug
    app.run(debug=True, host='0.0.0.0', port=5000)

@app.route('/start_processing', methods=['POST'])
def start_processing():
    print(f"DEBUG: Start processing called. Session: {session}")  # Debug
    if 'logged_in' not in session or 'temp_records_file' not in session:
        flash('Please upload a valid file first', 'error')
        print(f"START_PROCESSING ERROR: No records or not logged in. Session: {session}")  # Debug
        return redirect(url_for('upload'))
    
    if processing_state['is_processing']:
        flash('Processing already in progress', 'error')
        print("START_PROCESSING ERROR: Processing already in progress")  # Debug
        return redirect(url_for('upload'))
    
    # Load records from temporary file
    temp_filepath = session['temp_records_file']
    print(f"DEBUG: Loading records from temp file: {temp_filepath}")  # Debug
    try:
        with open(temp_filepath, 'r') as f:
            records = json.load(f)
        print(f"DEBUG: Loaded {len(records)} records from temp file")  # Debug
    except Exception as e:
        flash(f"Failed to load records: {str(e)}", 'error')
        print(f"START_PROCESSING ERROR: Failed to load records from {temp_filepath} - {str(e)}")  # Debug
        return redirect(url_for('upload'))
    
    processing_state['is_processing'] = True
    processing_state['progress'] = 0
    processing_state['logs'] = []
    processing_state['successful_count'] = 0
    processing_state['total_records'] = len(records)
    processing_state['current_aadhaar'] = ''
    
    print(f"START_PROCESSING: Starting thread with {len(records)} records")  # Debug
    threading.Thread(target=run_processing, args=(records,), daemon=True).start()
    return redirect(url_for('upload'))

@app.route('/logs')
def get_logs():
    print(f"DEBUG: Fetching logs. Processing state: {processing_state}")  # Debug
    return jsonify({
        'logs': processing_state['logs'],
        'progress': processing_state['progress'],
        'is_processing': processing_state['is_processing'],
        'successful_count': processing_state['successful_count'],
        'total_records': processing_state['total_records'],
        'current_aadhaar': processing_state['current_aadhaar']
    })

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'xlsx', 'xls'}

def run_processing(records):
    try:
        log_message("Starting Playwright processing", "info")
        # Track Aadhaar status for Excel output
        aadhaar_status = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=0, args=["--start-maximized"])
            context = browser.new_context(viewport={"width": 1366, "height": 768}, ignore_https_errors=True)
            page = context.new_page()

            log_message("Opening website...", "info")
            print("DEBUG: Navigating to https://fasalrin.gov.in/login")  # Debug
            page.goto('https://fasalrin.gov.in/login', timeout=60000)
            time.sleep(3)

            print("DEBUG: Waiting for manual login")  # Debug
            if not manual_login(page):
                raise Exception("Login failed")

            successful_count = 0
            for i, record in enumerate(records):
                aadhaar = record['Aadhar No']
                processing_state['current_aadhaar'] = aadhaar
                log_message(f"Processing {aadhaar} ({i+1}/{len(records)})", "info")
                print(f"DEBUG: Processing Aadhaar {aadhaar} ({i+1}/{len(records)})")  # Debug
                processing_state['progress'] = ((i + 1) / len(records)) * 100

                try:
                    if i > 0:
                        print("DEBUG: Navigating to dashboard")  # Debug
                        page.goto('https://fasalrin.gov.in/dashboard')
                        time.sleep(3)

                    success = process_single_application(page, aadhaar)
                    status = "Success" if success else "Failure"
                    aadhaar_status.append({"Aadhar No": aadhaar, "Status": status})
                    if success:
                        successful_count += 1
                        processing_state['successful_count'] = successful_count
                        log_message(f"Success: {aadhaar}", "success")
                        print(f"DEBUG: Success for Aadhaar {aadhaar}")  # Debug
                    else:
                        log_message(f"Failed: {aadhaar}", "error")
                        print(f"DEBUG: Failed for Aadhaar {aadhaar}")  # Debug
                    time.sleep(2)
                except Exception as e:
                    log_message(f"Error: {aadhaar} - {str(e)}", "error")
                    print(f"DEBUG: Error for Aadhaar {aadhaar}: {str(e)}")  # Debug
                    page.screenshot(path=f"{app.config['SCREENSHOT_FOLDER']}/error_{aadhaar}.png")
            
            # Save Aadhaar status to Excel
            output_df = pd.DataFrame(aadhaar_status)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], f"processing_results_{timestamp}.xlsx")
            output_df.to_excel(output_path, index=False)
            log_message(f"📊 Excel output saved to: {output_path}", "success")
            print(f"DEBUG: Excel output saved to: {output_path}")  # Debug
            
            log_message(f"Completed: Processed {successful_count}/{len(records)} Aadhaar numbers", "success")
            print(f"DEBUG: Processing completed. {successful_count}/{len(records)} successful")  # Debug
            processing_state['is_processing'] = False
            browser.close()
    except Exception as e:
        log_message(f"Fatal error: {str(e)}", "error")
        print(f"DEBUG: Fatal error in processing: {str(e)}")  # Debug
        processing_state['is_processing'] = False

if __name__ == '__main__':
    print("DEBUG: Starting Flask app")  # Debug
    app.run(debug=True, host='0.0.0.0', port=5000)