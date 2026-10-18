"""Ovela Global Intelligence v0.7 — real web search + optional LLM, citations, no invented contact info."""
import os,json,re,sqlite3,datetime,ipaddress,urllib.parse,asyncio,base64,hmac
from pathlib import Path
import httpx
from fastapi import FastAPI,HTTPException,Header,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from pydantic import BaseModel,Field
from dotenv import load_dotenv
load_dotenv()
ROOT=Path(__file__).parent
DB=Path(os.getenv('DATABASE_PATH',str(ROOT/'ovela.sqlite3')))
DB.parent.mkdir(parents=True,exist_ok=True)
app=FastAPI(title='Ovela Global Opportunity Intelligence',version='0.8')

@app.middleware('http')
async def protect(request:Request,call_next):
    if request.url.path == '/health':
        return await call_next(request)
    username=os.getenv('APP_USERNAME','')
    password=os.getenv('APP_PASSWORD','')
    if not username or not password:
        return JSONResponse({'error':'APP_USERNAME ja APP_PASSWORD puuttuvat palvelimen asetuksista'},status_code=503)
    authorization=request.headers.get('authorization','')
    valid=False
    if authorization.startswith('Basic '):
        try:
            decoded=base64.b64decode(authorization[6:],validate=True).decode('utf-8')
            supplied_user, supplied_pass=decoded.split(':',1)
            valid=hmac.compare_digest(supplied_user,username) and hmac.compare_digest(supplied_pass,password)
        except (ValueError,UnicodeDecodeError):
            pass
    if not valid:
        return Response(status_code=401,headers={'WWW-Authenticate':'Basic realm="Ovela Private Pilot"','Cache-Control':'no-store'})
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    return response

@app.get('/health')
def health():return {'status':'ok','version':'0.8'}

def conn():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;return c
@app.on_event('startup')
def init():
 with conn() as c:
  c.execute('CREATE TABLE IF NOT EXISTS prospects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, country TEXT, website TEXT, fit_reason TEXT, risks TEXT, priority TEXT, confidence TEXT, researched_at TEXT, sources_json TEXT, contact_status TEXT, ownership_status TEXT, sales_potential TEXT, UNIQUE(name,country))')
  c.execute('CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, created_at TEXT, result_count INTEGER)')
  if c.execute('SELECT COUNT(*) FROM prospects').fetchone()[0]==0:
   for lead in json.loads((ROOT/'leads.json').read_text(encoding='utf-8')):
    c.execute('INSERT OR IGNORE INTO prospects(name,country,website,fit_reason,risks,priority,confidence,researched_at,sources_json,contact_status,ownership_status,sales_potential) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(lead['name'],lead.get('country',''),lead.get('website',''),lead.get('fit_reason',''),lead.get('open_questions',''),lead.get('priority',''), 'VANHA ESIMERKKIAINEISTO — ei tuoreesti tarkistettu',lead.get('source_checked',''),json.dumps([lead.get('source_url')] if lead.get('source_url') else []), 'Ei varmennettu','Ei varmennettu','Ei laskettu'))

class SearchRequest(BaseModel):
 product:str=Field(min_length=3,max_length=500)
 countries:list[str]=Field(default_factory=lambda:['Germany','Poland','France'],min_length=1,max_length=5)
 customer_type:str=Field(default='premium pet food distributor',max_length=150)
 per_country:int=Field(default=5,ge=1,le=10)

@app.get('/')
def index():return FileResponse(ROOT/'index.html')
@app.get('/api/status')
def status():return {'search_enabled':bool(os.getenv('BRAVE_SEARCH_API_KEY')),'ai_enabled':bool(os.getenv('OPENAI_API_KEY')),'mode':'live' if os.getenv('BRAVE_SEARCH_API_KEY') else 'demo','note':'Hakutulokset eivät todista ostohalukkuutta. AI-analyysi ei varmista faktoja.'}
@app.get('/api/prospects')
def prospects():
 with conn() as c: rows=[dict(r) for r in c.execute('SELECT * FROM prospects ORDER BY id DESC')]
 for r in rows:r['sources']=json.loads(r.pop('sources_json') or '[]')
 return rows

def safe_url(url):
 try:
  u=urllib.parse.urlparse(url)
  if u.scheme!='https' or not u.hostname or u.username or u.password:return None
  # No fetch of these URLs by server: only preserve returned citations
  return url if len(url)<2000 else None
 except Exception:return None

async def brave_search(query,count):
 key=os.getenv('BRAVE_SEARCH_API_KEY')
 if not key:raise HTTPException(503,'Aseta BRAVE_SEARCH_API_KEY palvelimen .env-tiedostoon. Ilman avainta käytettävissä vain demo.')
 async with httpx.AsyncClient(timeout=20) as client:
  res=await client.get('https://api.search.brave.com/res/v1/web/search',params={'q':query,'count':min(count,10)},headers={'X-Subscription-Token':key,'Accept':'application/json'})
  if res.status_code!=200:raise HTTPException(502,f'Hakupalvelu palautti HTTP {res.status_code}')
  return [{'title':x.get('title',''),'url':safe_url(x.get('url','')),'snippet':re.sub('<[^>]+>','',x.get('description',''))[:800]} for x in res.json().get('web',{}).get('results',[]) if safe_url(x.get('url',''))]

async def ai_assess(product,market,results):
 if not os.getenv('OPENAI_API_KEY'):return None
 from openai import AsyncOpenAI
 client=AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
 instruction="""You are a cautious B2B market analyst. Only use supplied web snippets. Return JSON object with "candidates" list (max 5). Each item: name, country, website, fit_reason, risks, priority (A/B/C), confidence (low/medium), sources (URLs from provided snippets ONLY), contact_status (always "Not verified"), ownership_status (always "Not verified"), sales_potential (always "Not estimable without purchase data"). Do not invent company names, ownership, contacts, LinkedIn URLs, revenue, buying intent, cold-chain capability or purchasing volumes. If evidence insufficient, omit the candidate. No markdown."""
 resp=await client.chat.completions.create(model=os.getenv('OPENAI_MODEL','gpt-4.1-mini'),temperature=0,response_format={'type':'json_object'},messages=[{'role':'system','content':instruction},{'role':'user','content':json.dumps({'product':product,'market':market,'search_results':results},ensure_ascii=False)}],timeout=45)
 try:return json.loads(resp.choices[0].message.content)
 except Exception:return {'candidates':[]}

@app.post('/api/research')
async def research(request:SearchRequest,x_admin_token:str|None=Header(default=None)):
 expected=os.getenv('ADMIN_TOKEN')
 if not expected or expected=='change-this-long-random-token' or x_admin_token!=expected:raise HTTPException(401,'ADMIN_TOKEN puuttuu tai on väärä; vaihda oletusarvo.')
 output=[]
 for country in request.countries:
  if not country or len(country)>80 or any(c in country for c in '<>\\'):
   raise HTTPException(422,'Virheellinen kohdemaa')
  query=f'{request.customer_type} {country} wholesale B2B distribution {request.product[:80]}'
  hits=await brave_search(query,request.per_country)
  assessment=await ai_assess(request.product,country,hits)
  candidates=[]
  if assessment:
   valid={h['url'] for h in hits}
   for p in assessment.get('candidates',[]):
    sources=[s for s in p.get('sources',[]) if s in valid]
    if not sources or not p.get('name'):continue
    candidates.append({'name':str(p['name'])[:180],'country':country,'website':str(p.get('website',''))[:500],'fit_reason':str(p.get('fit_reason',''))[:1500],'risks':str(p.get('risks',''))[:1000],'priority':p.get('priority','C') if p.get('priority') in ['A','B','C'] else 'C','confidence':'AI-arvio / ei itsenäisesti vahvistettu','sources':sources,'contact_status':'Ei varmennettu','ownership_status':'Ei varmennettu','sales_potential':'Ei laskettavissa ilman ostodataa'})
  else:
   for h in hits:
    candidates.append({'name':h['title'][:180],'country':country,'website':h['url'],'fit_reason':'Hakutulos, ei vahvistettu soveltuvuus: '+h['snippet'][:450],'risks':'Vaatii yrityksen identiteetin, jakeluroolin ja kylmäketjun tarkistuksen','priority':'C','confidence':'Hakutulos / ei vahvistettu yritykseksi','sources':[h['url']],'contact_status':'Ei varmennettu','ownership_status':'Ei varmennettu','sales_potential':'Ei laskettavissa ilman ostodataa'})
  with conn() as c:
   c.execute('INSERT INTO searches(query,created_at,result_count) VALUES(?,?,?)',(query,datetime.datetime.now(datetime.timezone.utc).isoformat(),len(candidates)))
   for p in candidates:
    c.execute('INSERT INTO prospects(name,country,website,fit_reason,risks,priority,confidence,researched_at,sources_json,contact_status,ownership_status,sales_potential) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name,country) DO UPDATE SET website=excluded.website,fit_reason=excluded.fit_reason,risks=excluded.risks,priority=excluded.priority,confidence=excluded.confidence,researched_at=excluded.researched_at,sources_json=excluded.sources_json',(p['name'],p['country'],p['website'],p['fit_reason'],p['risks'],p['priority'],p['confidence'],datetime.datetime.now(datetime.timezone.utc).isoformat(),json.dumps(p['sources']),p['contact_status'],p['ownership_status'],p['sales_potential']))
  output.extend(candidates)
 return {'count':len(output),'candidates':output,'mode':'ai_assisted' if os.getenv('OPENAI_API_KEY') else 'search_only'}
