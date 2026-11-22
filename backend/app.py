from flask import Flask, render_template, request, redirect, url_for, jsonify
from resume_parser import extract_text
from utils import load_candidates, save_candidates
from dotenv import load_dotenv
import datetime
import os
import uuid
import requests
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load environment variables
load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_FOLDER = os.path.join(BASE_DIR, "frontend")

app = Flask(
    __name__,
    template_folder=FRONTEND_FOLDER,
    static_folder=os.path.join(FRONTEND_FOLDER, "static")
)

UPLOAD_FOLDER = os.path.join(BASE_DIR, "data")
os.makedirs(os.path.join(UPLOAD_FOLDER, "resumes"), exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def screen_resume_locally(resume_text, jd_text):
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=gemini_key)
        prompt = (
            "You are a technical recruiter. Evaluate the candidate resume against the job description and return your assessment strictly as a JSON object.\n\n"
            "=== JOB DESCRIPTION ===\n" + jd_text + "\n\n"
            "=== CANDIDATE RESUME ===\n" + resume_text + "\n\n"
            "Rules:\n"
            "- Extract candidate_name and email (empty string if none).\n"
            "- Extract phone number or set 'N/A'.\n"
            "- Give score 0-100 for JD match.\n"
            "- tier is strictly one of: 'Strong Fit', 'Possible Fit', 'Not Fit'.\n"
            "- List matched_skills (list of strings) and missing_skills (list of strings).\n"
            "- reasoning must cite exactly what matched or is missing.\n"
            "- recommendation is a short next step for the recruiter.\n\n"
            "Return JSON with keys: candidate_name, email, phone, score, tier, matched_skills, missing_skills, reasoning, recommendation"
        )
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt
        )
        txt = response.text.strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*", "", txt)
            txt = re.sub(r"\s*```$", "", txt)
        import json
        return json.loads(txt)
    except Exception as e:
        print(f"Direct Gemini AI screening warning: {e}")
        return None


def extract_email_from_text(text):
    if not text:
        return None
    match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if match:
        return match.group(0)
    return None


@app.route("/")
def home():
    return render_template("about.html")


@app.route("/screener")
def screener():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    resumes = request.files.getlist("resume")
    jd_text = request.form.get("jd_text", "")
    job_title = request.form.get("job_title", "Backend Developer")

    candidates = load_candidates()
    n8n_url = os.getenv("N8N_WEBHOOK_URL")

    if not n8n_url:
        print("Error: N8N_WEBHOOK_URL is not set.")
        return redirect(url_for("dashboard"))

    # Prepare files and local text cache
    files = []
    opened_files = []
    resume_texts = {}
    
    try:
        for idx, resume in enumerate(resumes):
            if not resume.filename:
                continue
            resume_path = os.path.join(UPLOAD_FOLDER, "resumes", resume.filename)
            resume.save(resume_path)
            
            # Extract text locally for display in the local dashboard drawer if needed
            try:
                txt = extract_text(resume_path)
                resume_texts[resume.filename] = txt
            except Exception as extract_err:
                print(f"Local text extraction warning for {resume.filename}: {extract_err}")
                resume_texts[resume.filename] = ""

            # Prepare binary file for multipart/form-data post to n8n
            f = open(resume_path, "rb")
            opened_files.append(f)
            # Use 'data' prefix or similar key matching n8n binary mapping (any key is fine since Code node gets all file keys)
            files.append((f"resume_{idx}", (resume.filename, f, "application/pdf")))
            
        if files:
            data = {
                "job_description": jd_text,
                "recruiter_name": "ScreenSmart Recruiter",
                "recruiter_email": "recruiter@example.com"
            }
            
            print(f"Forwarding {len(files)} files to n8n webhook: {n8n_url}")
            response = requests.post(n8n_url, files=files, data=data, timeout=12)
            print(f"n8n webhook response code: {response.status_code}")
            
            n8n_processed = False
            if response.status_code == 200:
                response_data = response.json()
                data_obj = response_data
                if isinstance(response_data, list) and len(response_data) > 0:
                    data_obj = response_data[0]
                
                n8n_candidates = data_obj.get("candidates", [])
                if n8n_candidates:
                    n8n_processed = True
                    resume_keys = list(resume_texts.keys())
                    for idx, candidate in enumerate(n8n_candidates):
                        candidate_id = str(uuid.uuid4())
                        matched_skills = candidate.get("matched_skills") or []
                        missing_skills = candidate.get("missing_skills") or []
                        reasoning = candidate.get("reasoning", "")
                        recommendation = candidate.get("recommendation", "")
                        
                        rationale_lines = []
                        if reasoning:
                            rationale_lines.append(f"✓ AI Reasoning: {reasoning}")
                        if recommendation:
                            rationale_lines.append(f"✓ Recommendation: {recommendation}")
                        for skill in missing_skills:
                            rationale_lines.append(f"• Missing: {skill}")
                            
                        rationale = "\n".join(rationale_lines) if rationale_lines else "No justification provided."
                        
                        file_name = candidate.get("file_name", "")
                        cand_resume_text = resume_texts.get(file_name, "")
                        if not cand_resume_text and idx < len(resume_keys):
                            cand_resume_text = resume_texts[resume_keys[idx]]

                        tier = candidate.get("tier") or "Possible Fit"
                        slots = candidate.get("proposed_slots") or []
                        if tier == "Strong Fit" and not slots:
                            today = datetime.date.today()
                            slots = [
                                (today + datetime.timedelta(days=1)).strftime("%a, %b %d, %Y at 10:00 AM"),
                                (today + datetime.timedelta(days=2)).strftime("%a, %b %d, %Y at 02:00 PM"),
                                (today + datetime.timedelta(days=3)).strftime("%a, %b %d, %Y at 04:00 PM")
                            ]

                        candidate_data = {
                            "id": candidate_id,
                            "name": candidate.get("candidate_name") or os.path.splitext(file_name)[0] or "Unknown Candidate",
                            "email": candidate.get("email") or "candidate@example.com",
                            "phone": candidate.get("phone") or "N/A",
                            "score": candidate.get("score") or 0,
                            "tier": tier,
                            "strengths": matched_skills,
                            "missing_requirements": missing_skills,
                            "rationale": rationale,
                            "status": candidate.get("status") or ("Invited" if tier == "Strong Fit" else "Pending"),
                            "interview_time": candidate.get("interview_time") or "",
                            "proposed_slots": slots,
                            "resume_text": cand_resume_text
                        }
                        candidates.append(candidate_data)
                    
                    save_candidates(candidates)
                    print(f"Successfully added {len(n8n_candidates)} candidates from n8n response.")
            else:
                print(f"n8n webhook returned status {response.status_code}: {response.text}")

            if not n8n_processed and resume_texts:
                print("n8n webhook did not return candidates. Using Gemini AI screening fallback...")
                for fname, txt in resume_texts.items():
                    parsed = screen_resume_locally(txt, jd_text)
                    candidate_id = str(uuid.uuid4())
                    if parsed:
                        matched_skills = parsed.get("matched_skills") or []
                        missing_skills = parsed.get("missing_skills") or []
                        reasoning = parsed.get("reasoning", "")
                        recommendation = parsed.get("recommendation", "")
                        tier = parsed.get("tier") or "Possible Fit"

                        rationale_lines = []
                        if reasoning:
                            rationale_lines.append(f"✓ AI Reasoning: {reasoning}")
                        if recommendation:
                            rationale_lines.append(f"✓ Recommendation: {recommendation}")
                        for skill in missing_skills:
                            rationale_lines.append(f"• Missing: {skill}")
                        rationale = "\n".join(rationale_lines) if rationale_lines else "Evaluated by Gemini AI."

                        today = datetime.date.today()
                        slots = []
                        if tier == "Strong Fit":
                            slots = [
                                (today + datetime.timedelta(days=1)).strftime("%a, %b %d, %Y at 10:00 AM"),
                                (today + datetime.timedelta(days=2)).strftime("%a, %b %d, %Y at 02:00 PM"),
                                (today + datetime.timedelta(days=3)).strftime("%a, %b %d, %Y at 04:00 PM")
                            ]

                        c_email = parsed.get("email") or extract_email_from_text(txt) or "candidate@example.com"
                        c_name = parsed.get("candidate_name") or os.path.splitext(fname)[0]

                        candidate_data = {
                            "id": candidate_id,
                            "name": c_name,
                            "email": c_email,
                            "phone": parsed.get("phone") or "N/A",
                            "score": parsed.get("score") or 0,
                            "tier": tier,
                            "strengths": matched_skills,
                            "missing_requirements": missing_skills,
                            "rationale": rationale,
                            "status": "Invited" if tier == "Strong Fit" else "Pending",
                            "interview_time": "",
                            "proposed_slots": slots,
                            "resume_text": txt
                        }
                    else:
                        candidate_data = {
                            "id": candidate_id,
                            "name": os.path.splitext(fname)[0],
                            "email": extract_email_from_text(txt) or "candidate@example.com",
                            "phone": "N/A",
                            "score": 0,
                            "tier": "Not Fit",
                            "strengths": [],
                            "missing_requirements": ["AI analysis failed"],
                            "rationale": "Automated screening fallback failed.",
                            "status": "Pending",
                            "interview_time": "",
                            "proposed_slots": [],
                            "resume_text": txt
                        }
                    candidates.append(candidate_data)
                save_candidates(candidates)
                print(f"Fallback added {len(resume_texts)} candidates.")
                
    except Exception as e:
        print(f"Error processing files or contacting n8n webhook: {e}")
    finally:
        for f in opened_files:
            f.close()

    if len(resumes) == 1 and candidates:
        return redirect(url_for("candidate_dashboard", candidate_id=candidates[-1]["id"]))
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    candidates = load_candidates()
    candidates_sorted = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)
    return render_template("dashboard.html", candidates=candidates_sorted, selected_candidate_id=None)


@app.route("/dashboard/<candidate_id>")
def candidate_dashboard(candidate_id):
    candidates = load_candidates()
    candidates_sorted = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)
    candidate = next((c for c in candidates if c["id"] == candidate_id), None)
    if not candidate:
        return "Candidate not found", 404
    return render_template("dashboard.html", candidates=candidates_sorted, selected_candidate_id=candidate_id)


def send_gmail(to_email, subject, body_text):
    sender_email = os.getenv("GMAIL_EMAIL")
    app_password = os.getenv("GMAIL_APP_PASSWORD")

    if not sender_email or not app_password or "your-email" in sender_email or "xxxx" in app_password:
        reason = "Gmail SMTP credentials not configured or using placeholders in .env."
        print(f"Email failed: {reason}")
        return False, reason

    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body_text, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, to_email, msg.as_string())
        server.quit()
        print(f"Success: Email sent to {to_email}")
        return True, "Email sent successfully"
    except Exception as e:
        print(f"Email failed: {e}")
        return False, str(e)


@app.route("/send_invite", methods=["POST"])
def send_invite():
    data = request.get_json()
    candidate_id = data.get("id")
    subject = data.get("subject", "Interview Invitation")
    body = data.get("body", "")

    if not candidate_id or not body:
        return jsonify({"success": False, "error": "Missing parameters"}), 400

    candidates = load_candidates()
    candidate = next((c for c in candidates if c["id"] == candidate_id), None)
    if not candidate:
        return jsonify({"success": False, "error": "Candidate not found"}), 404

    success, err_msg = send_gmail(candidate["email"], subject, body)
    
    if not success:
        return jsonify({"success": False, "error": f"SMTP sending failed: {err_msg}"}), 500

    candidate["status"] = "Interview Sent"
    save_candidates(candidates)
    
    return jsonify({"success": True, "email_sent": True})


@app.route("/schedule_interview", methods=["POST"])
def schedule_interview():
    data = request.get_json()
    candidate_id = data.get("id")
    interview_time = data.get("time")

    if not candidate_id or not interview_time:
        return jsonify({"success": False, "error": "Missing parameters"}), 400

    candidates = load_candidates()
    updated = False
    for c in candidates:
        if c["id"] == candidate_id:
            c["status"] = "Interview Scheduled"
            c["interview_time"] = interview_time
            updated = True
            break

    if updated:
        save_candidates(candidates)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Candidate not found"}), 404


@app.route("/clear_candidates", methods=["POST"])
def clear_candidates():
    save_candidates([])
    return redirect(url_for("dashboard"))


@app.route("/delete_candidate", methods=["POST"])
def delete_candidate():
    data = request.get_json()
    candidate_id = data.get("id")
    if not candidate_id:
        return jsonify({"success": False, "error": "Missing candidate id"}), 400
        
    candidates = load_candidates()
    new_candidates = [c for c in candidates if c["id"] != candidate_id]
    save_candidates(new_candidates)
    return jsonify({"success": True})


@app.route("/update_candidates_db", methods=["POST"])
def update_candidates_db():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"success": False, "error": "Expected a list of candidates"}), 400
    save_candidates(data)
    return jsonify({"success": True})


@app.route("/load_samples", methods=["POST"])
def load_samples():
    today = datetime.date.today()
    slots = [
        (today + datetime.timedelta(days=1)).strftime("%a, %b %d, %Y at 10:00 AM"),
        (today + datetime.timedelta(days=2)).strftime("%a, %b %d, %Y at 02:00 PM"),
        (today + datetime.timedelta(days=3)).strftime("%a, %b %d, %Y at 04:00 PM")
    ]
    
    samples = [
        {
            "id": "sample-1",
            "name": "Aarav Sharma",
            "email": "aarav@example.com",
            "phone": "555-0199",
            "score": 95,
            "tier": "Strong Fit",
            "rationale": "✓ 5+ years of Java and Spring Boot experience\n✓ Holds active AWS Solution Architect certification\n✓ Built high-performance microservices and REST APIs\n✗ Lacks Docker container configuration details",
            "strengths": ["Microservices design", "Java Spring core expert", "AWS certified"],
            "missing_requirements": ["Docker"],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "Aarav Sharma\nEmail: aarav@example.com\nExperience: 5 years Java / Spring Boot. AWS Solution Architect. Built high-traffic REST APIs."
        },
        {
            "id": "sample-2",
            "name": "Priya Patel",
            "email": "priya@example.com",
            "phone": "555-0143",
            "score": 78,
            "tier": "Possible Fit",
            "rationale": "✓ 3 years of Python and Flask experience\n✓ Strong SQL query optimization skills\n✗ Missing AWS cloud experience\n✗ No Docker/Kubernetes containerization",
            "strengths": ["Excellent Flask backend developer", "Strong SQL query optimization skills"],
            "missing_requirements": ["AWS Certification", "Docker"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Priya Patel\nEmail: priya@example.com\nBackend Developer with 3 years of experience. Stack: Python, Flask, Postgres SQL."
        },
        {
            "id": "sample-3",
            "name": "David Miller",
            "email": "david.m@example.com",
            "phone": "555-0182",
            "score": 71,
            "tier": "Possible Fit",
            "rationale": "✓ 2 years of JavaScript and web development\n✓ Strong database query logic with SQL\n✗ Lacks Python production backend experience\n✗ No cloud native infrastructure experience",
            "strengths": ["High motivation & quick learning curve", "Good database query logic"],
            "missing_requirements": ["Python", "Flask", "AWS"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "David Miller\nEmail: david.m@example.com\nSelf-taught developer. 2 years building frontend apps with React & NodeJS."
        },
        {
            "id": "sample-4",
            "name": "Emma Thompson",
            "email": "emma.t@example.com",
            "phone": "555-0155",
            "score": 88,
            "tier": "Strong Fit",
            "rationale": "✓ 4 years of Python & Flask experience\n✓ Proficient with Docker containerization\n✓ REST API development experience\n✗ Missing cloud provider (AWS) experience\n✗ 1-year career gap (justified by profile)",
            "strengths": ["Proficient in Docker containers", "Excellent database schema design"],
            "missing_requirements": ["AWS"],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "Emma Thompson\nEmail: emma.t@example.com\nSenior Backend Dev. Python, Flask, Docker, Postgres. (Career gap 2024-2025)."
        },
        {
            "id": "sample-5",
            "name": "Robert Chen",
            "email": "rob.c@example.com",
            "phone": "555-0121",
            "score": 68,
            "tier": "Possible Fit",
            "rationale": "✓ 2 years of core Python scripting\n✓ Basic SQL database skills\n✗ Lacks framework (Flask) experience\n✗ Poor resume formatting makes extraction difficult\n✗ Missing containerization/cloud skills",
            "strengths": ["Core python programming knowledge"],
            "missing_requirements": ["Flask", "Docker", "AWS"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Robert Chen - python - sql - db developer - 2 years exp - rob@email.com"
        },
        {
            "id": "sample-6",
            "name": "Aisha Patel",
            "email": "aisha@example.com",
            "phone": "555-0177",
            "score": 70,
            "tier": "Possible Fit",
            "rationale": "✓ Graduate in Computer Science\n✓ Completed Python & REST API internship projects\n✓ Solid SQL foundations\n✗ Missing production-level backend experience\n✗ Missing AWS cloud/Docker experience",
            "strengths": ["High academic record", "Solid data structure foundations"],
            "missing_requirements": ["AWS", "Docker", "Flask"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Aisha Patel\nEmail: aisha@example.com\nFresh Grad CS. Internship at Tech Corp building Python scripts."
        },
        {
            "id": "sample-7",
            "name": "Kevin Vance",
            "email": "kevin@example.com",
            "phone": "555-0112",
            "score": 45,
            "tier": "Not Fit",
            "rationale": "✓ Basic HTML editing skills\n✗ Lacks Python development experience (IT Support background)\n✗ Lacks Flask web framework\n✗ Lacks cloud infrastructure experience\n✗ Keyword stuffing without real project history",
            "strengths": ["Familiar with basic hardware and helpdesk tasks"],
            "missing_requirements": ["Python", "Flask", "AWS", "SQL", "Docker"],
            "status": "Rejected",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Kevin Vance\nSkills: Python AWS Docker Flask Spring Boot Java SQL React Angular\nExperience: IT Support Agent 3 years."
        },
        {
            "id": "sample-8",
            "name": "Clara Barton",
            "email": "clara@example.com",
            "phone": "555-0144",
            "score": 93,
            "tier": "Strong Fit",
            "rationale": "✓ 6 years of Python and Flask development\n✓ Proficient in Docker container setups\n✓ Cloud native deployment on AWS\n✓ Scaled REST APIs\n✗ Lacks TypeScript frontend experience",
            "strengths": ["Full stack versatility", "Advanced database optimization"],
            "missing_requirements": [],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "Clara Barton\nEmail: clara@example.com\n6 years backend dev. Expert in Flask, Python, AWS deployment."
        },
        {
            "id": "sample-9",
            "name": "James Wilson",
            "email": "james@example.com",
            "phone": "555-0188",
            "score": 91,
            "tier": "Strong Fit",
            "rationale": "✓ 12 years backend engineering and system architecture\n✓ Expert with Python, Flask, SQL, Docker, AWS\n✗ Overqualified for this mid-level engineer role",
            "strengths": ["Enterprise systems architecture", "Cloud infrastructure design"],
            "missing_requirements": [],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "James Wilson\nPrincipal Architect. 12 years exp. Expert in Cloud and Backend systems."
        },
        {
            "id": "sample-10",
            "name": "Rahul Robinson",
            "email": "rahul@example.com",
            "phone": "555-0103",
            "score": 35,
            "tier": "Not Fit",
            "rationale": "✓ 4 years SQL experience\n✗ Wrong stack (exclusive PHP/WordPress development)\n✗ No Python or Flask backend experience\n✗ No containerization (Docker) or AWS cloud experience",
            "strengths": ["CMS development"],
            "missing_requirements": ["Python", "Flask", "Docker", "AWS"],
            "status": "Rejected",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Rahul Robinson\nWordPress and PHP expert. 4 years building plugins."
        },
        {
            "id": "sample-11",
            "name": "Linus Torvalds",
            "email": "linus@example.com",
            "phone": "555-0190",
            "score": 90,
            "tier": "Strong Fit",
            "rationale": "✓ 20+ years low-level systems architecture\n✓ Expert in Docker configurations and Linux\n✓ Strong SQL database knowledge\n✗ Lacks Flask web framework specific experience",
            "strengths": ["Low-level systems expertise", "Docker configuration master"],
            "missing_requirements": ["Flask"],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "Linus Torvalds\nLinux Founder. Git Creator. Systems Architect."
        },
        {
            "id": "sample-12",
            "name": "Chloe Adams",
            "email": "chloe@example.com",
            "phone": "555-0161",
            "score": 64,
            "tier": "Possible Fit",
            "rationale": "✓ B.S. CS Graduate\n✓ Basic Python project experience\n✗ Zero full-time production engineering experience\n✗ Missing Flask frameworks\n✗ Missing Docker/AWS cloud deployments",
            "strengths": ["Academically strong in database design"],
            "missing_requirements": ["Flask", "Docker", "AWS"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Chloe Adams\nRecent CS Graduate. Familiar with Python scripting."
        },
        {
            "id": "sample-13",
            "name": "Arthur Pendragon",
            "email": "arthur@example.com",
            "phone": "555-0125",
            "score": 40,
            "tier": "Not Fit",
            "rationale": "✓ Basic HTML familiarity\n✗ Career changer (History teacher)\n✗ Only 2-week coding bootcamp experience\n✗ Lacks engineering foundation and backend skills",
            "strengths": ["Great communication and leadership"],
            "missing_requirements": ["Python", "Flask", "Docker", "AWS", "SQL"],
            "status": "Rejected",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Arthur Pendragon\nFormer History Teacher. Coding Bootcamp graduate."
        },
        {
            "id": "sample-14",
            "name": "Jessica Alba",
            "email": "jessica@example.com",
            "phone": "555-0130",
            "score": 76,
            "tier": "Possible Fit",
            "rationale": "✓ 3 years React and frontend JavaScript experience\n✓ REST API integration skills\n✗ Lacks Python development skills\n✗ Missing backend framework (Flask) experience\n✗ Lacks database and SQL design",
            "strengths": ["Stellar frontend interfaces", "REST API integration experience"],
            "missing_requirements": ["Python", "Flask", "SQL"],
            "status": "Pending",
            "interview_time": "",
            "proposed_slots": [],
            "resume_text": "Jessica Alba\nFrontend Developer. Stack: React, JavaScript, APIs."
        },
        {
            "id": "sample-15",
            "name": "Bruce Wayne",
            "email": "bruce@example.com",
            "phone": "555-0100",
            "score": 96,
            "tier": "Strong Fit",
            "rationale": "✓ 8 years secure cloud systems engineering\n✓ Expert in Python, Flask, Docker, AWS\n✓ Built secure REST APIs\n✓ Flawless technical alignment with JD",
            "strengths": ["Advanced cloud security architectures", "Flawless technical matching"],
            "missing_requirements": [],
            "status": "Interview Scheduled",
            "interview_time": slots[0],
            "proposed_slots": slots,
            "resume_text": "Bruce Wayne\nEmail: WayneTech@Gotham.com\n8 years building secure cloud APIs. Python, AWS, Docker."
        }
    ]
    
    save_candidates(samples)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True, use_reloader=False)
