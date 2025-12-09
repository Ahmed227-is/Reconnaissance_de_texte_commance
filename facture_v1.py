"""
Flask App - Système de Re-commande Intelligente
Permet d'uploader une ancienne commande (PDF/Image) et de la rééditer facilement
"""

import os
import re
import json
import ast
import base64
import requests
from pathlib import Path
from werkzeug.utils import secure_filename
import pandas as pd
from flask import Flask, request, jsonify, send_file
from io import BytesIO
from datetime import datetime

# Import ReportLab
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    print("⚠️  ReportLab not installed. PDF export will not work.")
    print("   Install with: pip install reportlab")

# ---------- Configuration ----------
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your-secret-key-change-this'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'pdf'}

# Create upload folder if not exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# OpenRouter Config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "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")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.0-flash-001"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json"
}

# ---------- Robust JSON Parsing ----------
def clean_markdown_json(text: str) -> str:
    """Supprime les balises markdown comme ```json et ```"""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text.strip())
    return text.strip()

def extract_first_json_candidate(text: str) -> str:
    """Extrait le premier bloc JSON du texte"""
    text = clean_markdown_json(text)
    pattern = re.compile(r'(\{.*\}|\[.*\])', re.DOTALL)
    m = pattern.search(text)
    if not m:
        raise ValueError("No JSON-like block found in text.")
    return m.group(1).strip()

def remove_trailing_commas(s: str) -> str:
    """Supprime les virgules avant } ou ]"""
    return re.sub(r',\s*(\}|])', r'\1', s)

def try_json_loads(s: str):
    return json.loads(s)

def try_ast_literal_eval(s: str):
    s2 = re.sub(r'\bnull\b', 'None', s, flags=re.IGNORECASE)
    s2 = re.sub(r'\btrue\b', 'True', s2, flags=re.IGNORECASE)
    s2 = re.sub(r'\bfalse\b', 'False', s2, flags=re.IGNORECASE)
    return ast.literal_eval(s2)

def tolerant_parse_json_from_text(raw_text: str):
    """Parse JSON avec plusieurs stratégies de fallback"""
    cleaned_text = clean_markdown_json(raw_text)
    
    # Try direct parse
    try:
        return try_json_loads(cleaned_text)
    except:
        pass
    
    try:
        return try_json_loads(raw_text)
    except:
        pass
    
    # Extract candidate
    try:
        candidate = extract_first_json_candidate(raw_text)
    except ValueError as e:
        raise ValueError(f"No JSON block found: {str(e)}")
    
    # Try various strategies
    for strategy in [
        lambda: try_json_loads(candidate),
        lambda: try_json_loads(remove_trailing_commas(candidate)),
        lambda: try_ast_literal_eval(candidate),
        lambda: try_json_loads(remove_trailing_commas(re.sub(r"(?<!\\)\'", '"', candidate)))
    ]:
        try:
            return strategy()
        except:
            continue
    
    raise ValueError(f"Failed to parse JSON. Snippet: {candidate[:500]}")

def normalize_number(s):
    """Normalise les nombres européens (5 000,0 -> 5000.0)"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace('\u00A0', ' ').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    else:
        s = s.replace(',', '.')
    s = re.sub(r'[^\d\.\-]', '', s)
    try:
        return float(s) if s != '' else None
    except ValueError:
        return None

# ---------- File Handling ----------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def encode_file_to_dataurl(file_path: str) -> str:
    """Encode un fichier en data URL base64"""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    b = p.read_bytes()
    b64 = base64.b64encode(b).decode("utf-8")
    
    suffix = p.suffix.lower()
    mime_types = {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.webp': 'image/webp',
        '.gif': 'image/gif',
        '.pdf': 'application/pdf'
    }
    mime = mime_types.get(suffix, 'image/jpeg')
    
    return f"data:{mime};base64,{b64}"

# ---------- LLM Extraction ----------
SYSTEM_MSG = {
    "role": "system",
    "content": (
        "You are a precise extractor specialized in order/invoice tables. "
        "You MUST respond ONLY with a single valid JSON object (no markdown, no commentary). "
        "If a value cannot be confidently extracted, use null. "
        "Numeric values must use dot as decimal separator (e.g., 5000.0). "
        "Quantities must be integers. Percent rates must be numeric (e.g., 20.0 for 20%)."
    )
}

USER_MSG_TEMPLATE = """Context:
You receive a photographed or scanned order table with columns:
- 'Produit' (product name)
- 'Qté' (quantity, may use spaces/commas like '5 000,0')
- 'P.U. H.T.' (unit price, may use commas)
- 'Total H.T.' (line total)
- 'Taux TVA' (VAT rate, e.g., '20,00%')

Task: Extract and return EXACTLY a JSON object with:
- document_type: string (e.g., 'order')
- currency: string or null (e.g., 'EUR')
- extraction_confidence: number (0-1)
- items: array with:
    - Produit: string
    - Qté: integer or null
    - P.U. H.T.: number or null
    - Total H.T.: number or null
    - Taux TVA: number or null (as numeric, e.g., 20.0)
    - line_text: string (raw text)

Rules:
- Output ONLY valid JSON
- Normalize numbers (remove thousands separators, use dot)
- Keep items in document order
- If no items: return {{"document_type":"unknown","currency":null,"extraction_confidence":0,"items":[]}}
"""

def call_llm_extraction(file_dataurl: str):
    """Appelle le LLM pour extraire les données"""
    user_content = [
        {"type": "text", "text": USER_MSG_TEMPLATE},
        {"type": "image_url", "image_url": {"url": file_dataurl}}
    ]
    
    payload = {
        "model": MODEL,
        "messages": [SYSTEM_MSG, {"role": "user", "content": user_content}],
        "temperature": 0.0,
        "max_tokens": 2000
    }
    
    resp = requests.post(OPENROUTER_URL, headers=HEADERS, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()

def parse_llm_response(resp_json: dict):
    """Parse la réponse du LLM"""
    choices = resp_json.get("choices", [])
    if not choices:
        raise ValueError("No choices returned by LLM")
    
    message = choices[0].get("message") or choices[0].get("delta") or {}
    content = message.get("content")
    
    if isinstance(content, dict):
        raw = json.dumps(content)
    elif isinstance(content, str):
        raw = content
    else:
        raw = json.dumps(choices[0])
    
    return tolerant_parse_json_from_text(raw)

def extract_order_data(file_path: str):
    """Extraction complète d'un fichier de commande"""
    dataurl = encode_file_to_dataurl(file_path)
    resp_json = call_llm_extraction(dataurl)
    extracted = parse_llm_response(resp_json)
    
    # Normalize numeric fields
    items = extracted.get("items", [])
    for itm in items:
        if itm.get("Qté") is not None:
            itm["Qté"] = int(normalize_number(itm.get("Qté")) or 0)
        
        for field in ["P.U. H.T.", "Total H.T."]:
            if itm.get(field) is not None:
                itm[field] = normalize_number(itm.get(field))
        
        # Normalize Taux TVA
        t = itm.get("Taux TVA")
        if isinstance(t, str):
            t2 = t.replace(',', '.').strip()
            if '%' in t2:
                try:
                    itm["Taux TVA"] = float(t2.replace('%', '').strip())
                except:
                    itm["Taux TVA"] = None
            else:
                try:
                    itm["Taux TVA"] = float(re.sub(r'[^\d\.\-]', '', t2))
                except:
                    itm["Taux TVA"] = None
    
    extracted["items"] = items
    return extracted

# ---------- Routes ----------
@app.route('/')
def index():
    """Page d'accueil"""
    html = '''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Re-Commande Intelligente</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; }
        .header p { font-size: 1.1em; opacity: 0.9; }
        .content { padding: 40px; }
        .upload-zone {
            border: 3px dashed #667eea;
            border-radius: 15px;
            padding: 60px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
            background: #f8f9ff;
            position: relative;
        }
        .upload-zone:hover {
            border-color: #764ba2;
            background: #f0f2ff;
            transform: translateY(-2px);
        }
        .upload-zone.dragover {
            background: #e8ebff;
            border-color: #764ba2;
        }
        .upload-icon { font-size: 4em; margin-bottom: 20px; }
        #fileInput { display: none; }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 40px;
            border-radius: 25px;
            font-size: 1.1em;
            cursor: pointer;
            transition: transform 0.2s;
            margin: 10px;
        }
        .btn:hover { transform: scale(1.05); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .loading {
            display: none;
            text-align: center;
            padding: 40px;
        }
        .spinner {
            border: 4px solid #f3f3f3;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .table-container { display: none; margin-top: 30px; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background: #667eea;
            color: white;
            font-weight: 600;
        }
        tr:hover { background: #f8f9ff; }
        input[type="number"], input[type="text"] {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 0.95em;
        }
        .actions {
            margin-top: 30px;
            text-align: center;
            display: none;
        }
        .info-box {
            background: #f0f2ff;
            border-left: 4px solid #667eea;
            padding: 15px;
            margin: 20px 0;
            border-radius: 5px;
        }
        .error-box {
            background: #ffe0e0;
            border-left: 4px solid #e74c3c;
            padding: 15px;
            margin: 20px 0;
            border-radius: 5px;
            display: none;
        }
        .btn-delete {
            background: #e74c3c;
            color: white;
            border: none;
            padding: 5px 15px;
            border-radius: 5px;
            cursor: pointer;
        }
        .btn-add {
            background: #27ae60;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔄 Re-Commande Intelligente</h1>
            <p>Uploadez votre ancienne commande et modifiez-la facilement</p>
        </div>
        
        <div class="content">
            <div class="info-box">
                <strong>📋 Comment ça marche ?</strong><br>
                1. Uploadez une ancienne commande (PDF ou Image)<br>
                2. L'IA extrait automatiquement les produits, quantités et prix<br>
                3. Modifiez les quantités ou ajoutez/retirez des produits<br>
                4. Exportez votre nouvelle commande en Excel
            </div>

            <div class="error-box" id="errorBox"></div>
            
            <div class="upload-zone" id="uploadZone">
                <div class="upload-icon">📤</div>
                <h2>Glissez votre commande ici</h2>
                <p>ou cliquez pour sélectionner un fichier</p>
                <p style="margin-top: 10px; color: #666; font-size: 0.9em;">
                    Formats acceptés: PDF, PNG, JPG, WEBP
                </p>
            </div>
            <input type="file" id="fileInput" accept=".pdf,.png,.jpg,.jpeg,.webp">
            
            <div class="loading" id="loading">
                <div class="spinner"></div>
                <h3>🔍 Extraction en cours...</h3>
                <p>L'IA analyse votre document</p>
            </div>
            
            <div class="table-container" id="tableContainer">
                <h2>📊 Votre commande extraite</h2>
                <div id="metadata"></div>
                <button class="btn-add" onclick="addRow()">➕ Ajouter une ligne</button>
                <table id="orderTable">
                    <thead>
                        <tr>
                            <th>Produit</th>
                            <th>Qté</th>
                            <th>P.U. H.T.</th>
                            <th>Total H.T.</th>
                            <th>TVA %</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody"></tbody>
                </table>
                
                <div class="actions" id="actions">
                    <button class="btn" onclick="exportPDF()">📄 Exporter PDF</button>
                    <button class="btn" onclick="exportExcel()">📊 Exporter Excel</button>
                    <button class="btn" onclick="resetForm()">🔄 Nouvelle commande</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        let extractedData = null;
        
        console.log('Script loaded!');
        
        const uploadZone = document.getElementById('uploadZone');
        const fileInput = document.getElementById('fileInput');
        const loading = document.getElementById('loading');
        const tableContainer = document.getElementById('tableContainer');
        const errorBox = document.getElementById('errorBox');
        const actions = document.getElementById('actions');
        
        console.log('Elements:', {uploadZone, fileInput, loading});
        
        // Click handler
        if (uploadZone) {
            uploadZone.onclick = function(e) {
                console.log('Zone clicked!');
                e.preventDefault();
                if (fileInput) {
                    fileInput.click();
                    console.log('File input triggered');
                }
            };
        }
        
        // File input change
        if (fileInput) {
            fileInput.onchange = function(e) {
                console.log('File selected:', e.target.files);
                if (e.target.files && e.target.files.length > 0) {
                    handleFile(e.target.files[0]);
                }
            };
        }
        
        // Drag and drop
        if (uploadZone) {
            uploadZone.ondragover = function(e) {
                e.preventDefault();
                uploadZone.classList.add('dragover');
            };
            
            uploadZone.ondragleave = function(e) {
                e.preventDefault();
                uploadZone.classList.remove('dragover');
            };
            
            uploadZone.ondrop = function(e) {
                e.preventDefault();
                uploadZone.classList.remove('dragover');
                console.log('File dropped:', e.dataTransfer.files);
                if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                    handleFile(e.dataTransfer.files[0]);
                }
            };
        }
        
        function handleFile(file) {
            console.log('Handling file:', file.name);
            const formData = new FormData();
            formData.append('file', file);
            
            uploadZone.style.display = 'none';
            loading.style.display = 'block';
            errorBox.style.display = 'none';
            
            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(res => {
                console.log('Response:', res.status);
                return res.json();
            })
            .then(data => {
                console.log('Data received:', data);
                if (data.success) {
                    extractedData = data.data;
                    displayTable(data.data);
                } else {
                    showError(data.error || 'Erreur lors de l\\'extraction');
                }
            })
            .catch(err => {
                console.error('Error:', err);
                showError('Erreur réseau: ' + err.message);
            });
        }
        
        function displayTable(data) {
            console.log('Displaying table');
            loading.style.display = 'none';
            tableContainer.style.display = 'block';
            actions.style.display = 'block';
            
            const metadata = document.getElementById('metadata');
            metadata.innerHTML = `
                <div class="info-box">
                    <strong>📄 Type:</strong> ${data.document_type || 'N/A'} | 
                    <strong>💰 Devise:</strong> ${data.currency || 'N/A'} | 
                    <strong>🎯 Confiance:</strong> ${(data.extraction_confidence * 100).toFixed(0)}%
                </div>
            `;
            
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';
            
            data.items.forEach((item, idx) => {
                addRowToTable(item, idx);
            });
        }
        
        function addRowToTable(item, idx) {
            const tbody = document.getElementById('tableBody');
            const row = tbody.insertRow();
            row.innerHTML = `
                <td><input type="text" value="${item.Produit || ''}" onchange="updateRow(${idx}, 'Produit', this.value)"></td>
                <td><input type="number" value="${item.Qté || 0}" onchange="updateRow(${idx}, 'Qté', this.value)"></td>
                <td><input type="number" step="0.01" value="${item['P.U. H.T.'] || 0}" onchange="updateRow(${idx}, 'P.U. H.T.', this.value)"></td>
                <td><strong>${(item['Total H.T.'] || 0).toFixed(2)}</strong></td>
                <td><input type="number" step="0.01" value="${item['Taux TVA'] || 20}" onchange="updateRow(${idx}, 'Taux TVA', this.value)"></td>
                <td><button class="btn-delete" onclick="deleteRow(${idx})">🗑️</button></td>
            `;
        }
        
        function updateRow(idx, field, value) {
            if (!extractedData || !extractedData.items[idx]) return;
            
            extractedData.items[idx][field] = field === 'Produit' ? value : parseFloat(value) || 0;
            
            const item = extractedData.items[idx];
            if (item.Qté && item['P.U. H.T.']) {
                item['Total H.T.'] = item.Qté * item['P.U. H.T.'];
            }
            
            displayTable(extractedData);
        }
        
        function deleteRow(idx) {
            if (!extractedData) return;
            extractedData.items.splice(idx, 1);
            displayTable(extractedData);
        }
        
        function addRow() {
            if (!extractedData) return;
            extractedData.items.push({
                Produit: '',
                Qté: 0,
                'P.U. H.T.': 0,
                'Total H.T.': 0,
                'Taux TVA': 20
            });
            displayTable(extractedData);
        }
        
        function exportPDF() {
            console.log('Exporting PDF...');
            fetch('/export-pdf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(extractedData)
            })
            .then(res => res.blob())
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'bon_de_commande_' + new Date().getTime() + '.pdf';
                a.click();
                window.URL.revokeObjectURL(url);
            })
            .catch(err => {
                console.error('PDF export error:', err);
                alert('Erreur lors de l\\'export PDF: ' + err.message);
            });
        }
        
        function exportExcel() {
            fetch('/export-excel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(extractedData)
            })
            .then(res => res.blob())
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'commande_modifiee.xlsx';
                a.click();
            });
        }
        
        function resetForm() {
            uploadZone.style.display = 'block';
            tableContainer.style.display = 'none';
            actions.style.display = 'none';
            extractedData = null;
            fileInput.value = '';
        }
        
        function showError(message) {
            loading.style.display = 'none';
            uploadZone.style.display = 'block';
            errorBox.style.display = 'block';
            errorBox.innerHTML = `<strong>❌ Erreur:</strong> ${message}`;
        }
        
        console.log('All functions defined');
    </script>
</body>
</html>'''
    return html

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload et extraction automatique"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400
    
    try:
        # Save file
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Extract data
        extracted_data = extract_order_data(filepath)
        
        # Clean up
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'data': extracted_data
        })
    
    except Exception as e:
        return jsonify({
            'error': str(e),
            'type': type(e).__name__
        }), 500

@app.route('/export-excel', methods=['POST'])
def export_excel():
    """Export en Excel"""
    data = request.json
    
    if not data or 'items' not in data:
        return jsonify({'error': 'Invalid data'}), 400
    
    try:
        # Create DataFrame
        df = pd.DataFrame(data['items'])
        cols = ["Produit", "Qté", "P.U. H.T.", "Total H.T.", "Taux TVA"]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        df = df[cols]
        
        # Create Excel file in memory
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Commande')
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='commande_modifiee.xlsx'
        )
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    

# --- Couleurs et Définitions ---
COLOR_HEADER_BG_DARK = colors.HexColor('#6C6C6C')  # Gris foncé (Bon de commande)
COLOR_HEADER_BG_LIGHT = colors.HexColor('#D3D3D3') # Gris clair (N° CC)
COLOR_TABLE_HEADER_BG = colors.white
COLOR_TABLE_BORDER = colors.black 

# Fonction d'aide pour le formatage à la française (Ex: 5 000,0)
def format_fr_pdf(number, decimals=2):
    """Formate un nombre en string français (espace comme séparateur de milliers, virgule comme décimal)"""
    if number is None:
        return ""
    if not isinstance(number, (int, float)):
        try:
            number = float(number)
        except:
            return str(number)

    # Pour les quantités, on coupe à la virgule
    if decimals == 0:
        return f"{int(number):,}".replace(",", " ")
        
    # Sépare la partie entière de la partie décimale
    integer, decimal = divmod(number, 1)
    
    # Formate la partie entière avec espace comme séparateur de milliers
    integer_str = f"{int(integer):,}".replace(",", " ")
    
    # Formate la partie décimale
    decimal_str = f"{decimal:.{decimals}f}".split('.')[1]
    
    return f"{integer_str},{decimal_str}"

@app.route('/export-pdf', methods=['POST'])
def export_pdf():
    """
    Génère un Bon de Commande en PDF fidèle au modèle fourni,
    avec un numéro de commande et une date dynamiques.
    """
    if not REPORTLAB_AVAILABLE:
        return jsonify({'error': 'ReportLab library is not installed. Run: pip install reportlab'}), 500
        
    data = request.json
    
    if not data or 'items' not in data:
        return jsonify({'error': 'Invalid data or missing items'}), 400

    try:
        print("📄 Generating PDF...")
        
        # --- Données dynamiques ---
        now = datetime.now()
        current_date_fr = now.strftime("%d/%m/%Y")
        current_time_fr = now.strftime("%H:%M:%S")
        order_number = f"N° CC-{current_date_fr}-{current_time_fr}"
        
        print(f"   Order number: {order_number}")
        print(f"   Date: {current_date_fr}")
        
        # 1. Préparation du document
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.5*cm,
            rightMargin=1.5*cm,
            topMargin=1.5*cm,
            bottomMargin=1.5*cm
        )
        story = []
        styles = getSampleStyleSheet()

        # Styles personnalisés pour ReportLab
        styles.add(ParagraphStyle(name='RightNormal', alignment=TA_RIGHT, fontSize=10))
        styles.add(ParagraphStyle(name='RightBold', alignment=TA_RIGHT, fontName='Helvetica-Bold', fontSize=14))
        styles.add(ParagraphStyle(name='LeftBold14', alignment=TA_RIGHT, fontName='Helvetica-Bold', fontSize=14, textColor=colors.black))
        styles.add(ParagraphStyle(name='CenterBold14', alignment=TA_CENTER, fontName='Helvetica-Bold', fontSize=12, textColor=colors.black))
        styles.add(ParagraphStyle(name='BodyTextSmall', alignment=TA_RIGHT, fontSize=10))
        styles.add(ParagraphStyle(name='TableHead', fontSize=10, fontName='Helvetica-Bold', textColor=colors.black, alignment=TA_CENTER))
        styles.add(ParagraphStyle(name='TableData', fontSize=10, alignment=TA_CENTER))
        styles.add(ParagraphStyle(name='TableDataLeft', fontSize=10, alignment=TA_LEFT))
        styles.add(ParagraphStyle(name='TableDataRight', fontSize=10, alignment=TA_RIGHT))

        # Définir l'espace disponible (Largeur A4 - marges)
        A4_WIDTH = A4[0] - 3*cm
        COL_WIDTH_LEFT = 5.5*cm
        COL_WIDTH_RIGHT = A4_WIDTH - COL_WIDTH_LEFT 

        # --- 2. Construction de la grille principale de l'en-tête ---
        
        # Colonne de droite: Infos Entreprise (en haut)
        company_paragraphs = [
            Paragraph("<b>Dubard cosmétiques SAS</b>", styles['RightBold']),
            Paragraph("1 côte du Touron", styles['RightNormal']),
            Paragraph("01120 La Perche Ferté", styles['RightNormal']),
            Paragraph("05 45 45 45 45", styles['RightNormal']),
            Paragraph("contact@dubard.com", styles['RightNormal']),
            Paragraph(f"Siret : 452 452 452 00014", styles['RightNormal']),
            Paragraph(f"TVA intracomm : FR854212521255", styles['RightNormal']),
        ]
        company_table = Table([[c] for c in company_paragraphs], colWidths=[COL_WIDTH_RIGHT], hAlign='RIGHT')
        
        # Colonne de gauche: Logo (en haut)
        logo_content = Paragraph("<u><i>logo</i></u>", styles['CenterBold14'])
        
        # Bloc Bon de Commande (Colonne de gauche, milieu)
        title_block_content = [
            Paragraph("<b>Bon de commande</b>", ParagraphStyle('TitleWhite', fontSize=14, fontName='Helvetica-Bold', textColor=colors.white, alignment=TA_CENTER)),
            Paragraph(f"<b>{order_number}</b>", ParagraphStyle('NumWhite', fontSize=11, fontName='Helvetica-Bold', textColor=colors.black, alignment=TA_CENTER)),
        ]
        title_table = Table([[c] for c in title_block_content], colWidths=[COL_WIDTH_LEFT], rowHeights=[0.8*cm, 0.8*cm])
        title_style = TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), COLOR_HEADER_BG_DARK),
            ('TEXTCOLOR', (0, 0), (0, 0), colors.black),
            ('VALIGN', (0, 0), (0, 0), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (0, 0), 10),
            
            ('BACKGROUND', (0, 1), (0, 1), COLOR_HEADER_BG_LIGHT),
            ('TEXTCOLOR', (0, 1), (0, 1), colors.black),
            ('VALIGN', (0, 1), (0, 1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 1), (0, 1), 5),
            ('TOPPADDING', (0, 1), (0, 1), 5),
        ])
        title_table.setStyle(title_style)

        # Date (Colonne de gauche, bas)
        date_paragraph = Paragraph(f"<b><i>{current_date_fr}</i></b>", styles['CenterBold14'])
        date_table = Table([[date_paragraph]], colWidths=[COL_WIDTH_LEFT], hAlign='LEFT')
        date_table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('ALIGN', (0, 0), (0, 0), 'CENTER'),
            ('TOPPADDING', (0, 0), (0, 0), 5),
            ('BOTTOMPADDING', (0, 0), (0, 0), 5),
        ]))
        
        # Infos Fournisseur (Colonne de droite, milieu)
        supplier_paragraphs = [
            Paragraph("<b>Fournisseur</b>", styles['LeftBold14']),
            Paragraph("Parfums et Cie SARL", styles['BodyTextSmall']),
            Paragraph("1 route des Joliettes, 84250 Grans", styles['BodyTextSmall']),
            Paragraph("04 65 89 78 78", styles['BodyTextSmall']),
            Paragraph("l.bernais@bernais.com", styles['BodyTextSmall']),
        ]
        supplier_table = Table([[c] for c in supplier_paragraphs], colWidths=[COL_WIDTH_RIGHT], hAlign='RIGHT')
        supplier_table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('ALIGN', (0, 0), (0, 0), 'RIGHT'),
        ]))

        # Table d'agencement pour l'en-tête complet
        header_grid_data = [
            [logo_content, company_table],
            [title_table, Spacer(1, 1.6*cm)],
            [date_table, supplier_table],
        ]

        header_grid = Table(header_grid_data, colWidths=[COL_WIDTH_LEFT, COL_WIDTH_RIGHT], hAlign='RIGHT')
        header_grid.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(header_grid)
        story.append(Spacer(1, 0.5 * cm))
        
        # --- 3. Tableau des produits ---
        items = data.get('items', [])
        print(f"   Processing {len(items)} items...")
        
        table_data = [
            [
                Paragraph("<b>Liste des produits et services commandés</b>", styles['TableHead']),
                Paragraph("<b>Qté</b>", styles['TableHead']), 
                Paragraph("<b>P.U. H.T.</b>", styles['TableHead']), 
                Paragraph("<b>Total H.T.</b>", styles['TableHead']), 
                Paragraph("<b>Taux TVA</b>", styles['TableHead'])
            ]
        ]
        
        for item in items:
            produit = item.get("Produit", "")
            qte = item.get("Qté")
            pu_ht = item.get("P.U. H.T.")
            total_ht = item.get("Total H.T.")
            taux_tva = item.get("Taux TVA")
            
            table_data.append([
                Paragraph(produit, styles['TableDataLeft']),
                Paragraph(format_fr_pdf(qte, decimals=1), styles['TableData']),
                Paragraph(format_fr_pdf(pu_ht, decimals=1), styles['TableData']),
                Paragraph(format_fr_pdf(total_ht, decimals=1), styles['TableData']),
                Paragraph(f"{format_fr_pdf(taux_tva, decimals=2)}%", styles['TableData'])
            ])

        col_widths = [7.5*cm, 2*cm, 2.5*cm, 3*cm, 2*cm]
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        
        table_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), COLOR_TABLE_HEADER_BG), 
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('ALIGN', (0, 0), (0, 0), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'), 
            ('ALIGN', (0, 1), (0, -1), 'LEFT'), 
            ('LEFTPADDING', (0, 1), (0, -1), 10), 
            ('GRID', (0, 0), (-1, -1), 1, COLOR_TABLE_BORDER),
        ])
        
        table.setStyle(table_style)
        story.append(table)
        
        # --- 4. Construction et envoi du document ---
        print("   Building PDF document...")
        doc.build(story)
        buffer.seek(0)
        
        print("✅ PDF generated successfully!")
        
        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'bon_de_commande_{now.strftime("%Y%m%d_%H%M%S")}.pdf'
        )

    except Exception as e:
        import traceback
        print("❌ PDF generation error:")
        traceback.print_exc()
        return jsonify({'error': f'PDF generation failed: {str(e)}'}), 500

# ---------- Run ----------
if __name__ == '__main__':
    print("🚀 Starting Flask Re-Order Intelligence App...")
    print("📱 Open: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)