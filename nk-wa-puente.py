#!/usr/bin/env python3
"""
Puente Shopify -> Klaviyo para WhatsApp y SMS (sustituye a nk-wa-consent).

Por que existe: quien abandona el checkout escribe su telefono en Shopify, pero
Klaviyo solo lo recibe si compra. Sin telefono en el perfil, los flujos de
WhatsApp y SMS del checkout no pueden enviarle nada.

Que hace, en este orden:
  1. Lee en Shopify (SOLO LECTURA) los checkouts sin completar de las ultimas N horas.
  2. Si el perfil de Klaviyo de ese email no tiene telefono, se lo pone.
  3. Da de alta en WhatsApp (resto del mundo) o SMS (+1 y +61) a quien tenga
     telefono y NUNCA se haya suscrito. A quien se dio de baja no lo toca.
  4. Repasa tambien los perfiles con Checkout Started reciente que ya tienen
     telefono en Klaviyo (compradores y clientes que repiten).

Uso:
  ./nk-wa-puente                 prueba: no escribe nada, solo cuenta
  ./nk-wa-puente --apply         escribe en Klaviyo
  ./nk-wa-puente --horas 30      ventana (por defecto 48)
  ./nk-wa-puente --sms           incluye SMS (Klaviyo manda un SMS pidiendo confirmar)
  ./nk-wa-puente --max 300       tope de altas por ejecucion (por defecto 200)

Nunca imprime emails ni telefonos: solo cuentas.

Credenciales:
  Klaviyo  -> variable KLAVIYO_API_KEY o ~/.klaviyo-nubekids-key
  Shopify  -> variables SHOPIFY_CLIENT_ID y SHOPIFY_CLIENT_SECRET, o ~/.nubekids-app-credentials
"""
import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

TIENDA = "zd0x4v-ts.myshopify.com"
SHOPIFY_VER = os.environ.get("NK_API_VERSION", "2026-01")
KLAVIYO = "https://a.klaviyo.com/api"
REV = "2026-07-15"
LIST_ID = "VPZMDz"
METRIC_CHECKOUT = "X6hw3g"
MARCA_SMS = "nk_alta_sms_pedida"      # propiedad de perfil: fecha en que se pidio la confirmacion por SMS


def arg(nombre, defecto=None, tipo=str):
    if nombre in sys.argv:
        i = sys.argv.index(nombre)
        if i + 1 < len(sys.argv):
            return tipo(sys.argv[i + 1])
    return defecto


APLICAR = "--apply" in sys.argv
CON_SMS = "--sms" in sys.argv
HORAS = arg("--horas", 48, int)
TOPE = arg("--max", 200, int)
SMS_MAX_MIN = arg("--sms-max-min", 15, int)   # SMS solo si el checkout es reciente: asi entra desde el primer SMS (sale a los 20 min)

# ---------------------------------------------------------------- credenciales

def clave_klaviyo():
    k = os.environ.get("KLAVIYO_API_KEY", "").strip()
    if k:
        return k
    return (Path.home() / ".klaviyo-nubekids-key").read_text().strip()


def credenciales_shopify():
    cid = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    sec = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    datos = {}
    for linea in (Path.home() / ".nubekids-app-credentials").read_text().splitlines():
        if "=" in linea:
            k, v = linea.split("=", 1)
            datos[k.strip()] = v.strip()
    return datos["CLIENT_ID"], datos["CLIENT_SECRET"]


KEY = clave_klaviyo()

# ---------------------------------------------------------------- http

def http(method, url, headers, body=None, form=None):
    """Va por curl: el Python de macOS no trae certificados y urllib falla con SSL."""
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    # cabeceras y cuerpo en archivos temporales (permisos 0600): asi la clave no sale en `ps`
    fh = tempfile.NamedTemporaryFile("w", suffix=".hdr", delete=False)
    fh.write("\n".join(f"{k}: {v}" for k, v in headers.items()) + "\n")
    fh.close()
    fb = None
    cmd = ["curl", "-gsS", "--max-time", "90", "-X", method, url, "-H", "@" + fh.name, "-w", "\n__HTTP__%{http_code}"]
    if data is not None:
        fb = tempfile.NamedTemporaryFile("wb", suffix=".body", delete=False)
        fb.write(data)
        fb.close()
        cmd += ["--data-binary", "@" + fb.name]
    try:
        for intento in range(5):
            r = subprocess.run(cmd, capture_output=True, text=True)
            out, code = r.stdout, 0
            if "__HTTP__" in out:
                out, c = out.rsplit("__HTTP__", 1)
                code = int(c.strip() or 0)
            if code in (429, 0) or code >= 500:
                time.sleep(2 + 3 * intento)
                continue
            break
    finally:
        os.unlink(fh.name)
        if fb:
            os.unlink(fb.name)
    try:
        d = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        d = {"errors": [{"detail": out[:200]}]}
    return code, d


def limpia(x):
    """Quita emails y telefonos de un texto antes de imprimirlo (los registros pueden ser publicos)."""
    t = json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
    t = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<email>", t)
    return re.sub(r"\+?\d[\d ()-]{6,}\d", "<tel>", t)


def kl(method, path, body=None):
    url = path if path.startswith("https://") else f"{KLAVIYO}/{path}"
    h = {"Authorization": f"Klaviyo-API-Key {KEY}", "accept": "application/vnd.api+json", "revision": REV}
    if body is not None:
        h["content-type"] = "application/vnd.api+json"
    return http(method, url, h, body=body)


_token = None


def shopify(query):
    global _token
    if _token is None:
        cid, sec = credenciales_shopify()
        code, d = http("POST", f"https://{TIENDA}/admin/oauth/access_token",
                       {"Content-Type": "application/x-www-form-urlencoded"},
                       form={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec})
        if "access_token" not in d:
            sys.exit(f"ERROR Shopify: no hay token (HTTP {code})")
        _token = d["access_token"]
    code, d = http("POST", f"https://{TIENDA}/admin/api/{SHOPIFY_VER}/graphql.json",
                   {"X-Shopify-Access-Token": _token, "Content-Type": "application/json"},
                   body={"query": query})
    if d.get("errors"):
        sys.exit("ERROR Shopify: " + limpia(d["errors"])[:300])
    return d["data"]

# ---------------------------------------------------------------- telefonos

# pais -> (prefijo, longitud maxima del numero nacional sin el cero inicial)
PAISES = {
    "ES": ("34", 9), "PT": ("351", 9), "FR": ("33", 9), "IT": ("39", 11), "DE": ("49", 11),
    "AT": ("43", 13), "CH": ("41", 9), "BE": ("32", 9), "NL": ("31", 9), "LU": ("352", 9),
    "IE": ("353", 9), "GB": ("44", 10), "PL": ("48", 9), "RO": ("40", 9), "BG": ("359", 9),
    "GR": ("30", 10), "CY": ("357", 8), "MT": ("356", 8), "HU": ("36", 9), "CZ": ("420", 9),
    "SK": ("421", 9), "SI": ("386", 8), "HR": ("385", 9), "RS": ("381", 9), "UA": ("380", 9),
    "LT": ("370", 8), "LV": ("371", 8), "EE": ("372", 8), "FI": ("358", 10), "SE": ("46", 9),
    "DK": ("45", 8), "NO": ("47", 8), "IS": ("354", 7), "MX": ("52", 10), "CO": ("57", 10),
    "CL": ("56", 9), "AR": ("54", 11), "PE": ("51", 9), "US": ("1", 10), "CA": ("1", 10),
    "AU": ("61", 9), "NZ": ("64", 10), "IL": ("972", 9), "AE": ("971", 9), "TR": ("90", 10),
}

try:                                    # en GitHub se instala; en local puede no estar
    import phonenumbers
except Exception:
    phonenumbers = None


def e164(crudo, pais):
    """Devuelve el telefono en formato +XXXXXXXX o None si no se puede normalizar."""
    crudo = (crudo or "").strip()
    if not crudo:
        return None
    if phonenumbers is not None:
        try:
            n = phonenumbers.parse(crudo, None if crudo.startswith("+") else pais)
            if phonenumbers.is_valid_number(n):
                return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            pass
        return None
    d = re.sub(r"\D", "", crudo)
    if crudo.startswith("+"):
        return "+" + d if 8 <= len(d) <= 15 else None
    if crudo.startswith("00"):
        d = d[2:]
        return "+" + d if 8 <= len(d) <= 15 else None
    if pais not in PAISES:
        return None
    cc, nmax = PAISES[pais]
    if d.startswith(cc) and len(d) > nmax:
        return "+" + d if len(d) <= 15 else None
    if pais != "IT":
        d = d.lstrip("0")
    if not 7 <= len(d) <= nmax:
        return None
    return "+" + cc + d


def minutos_desde(iso):
    return (ahora - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60


def canal(tel):
    return "sms" if tel.startswith("+1") or tel.startswith("+61") else "whatsapp"

# ---------------------------------------------------------------- lectura

ahora = datetime.now(timezone.utc)
desde = ahora - timedelta(hours=HORAS)
desde_iso = desde.strftime("%Y-%m-%dT%H:%M:%SZ")
print(f"[{ahora.strftime('%Y-%m-%d %H:%M:%S')} UTC] ventana {HORAS} h | modo {'ESCRIBE' if APLICAR else 'PRUEBA (no escribe)'} | SMS {'si' if CON_SMS else 'no'}")

# 1. Shopify: checkouts sin completar
abandonos = {}          # email -> (telefono E.164, creado)
sin_normalizar = 0
total_shopify = sin_tel = 0
cursor = None
while True:
    despues = f'"{cursor}"' if cursor else "null"
    q = ('{ abandonedCheckouts(first: 200, after: %s, sortKey: CREATED_AT, reverse: true, query: "created_at:>=%s") '
         '{ edges { node { createdAt completedAt customer { defaultEmailAddress { emailAddress } defaultPhoneNumber { phoneNumber } } '
         'shippingAddress { countryCodeV2 phone } billingAddress { countryCodeV2 phone } } } pageInfo { hasNextPage endCursor } } }'
         % (despues, desde_iso))
    ac = shopify(q)["abandonedCheckouts"]
    for e in ac["edges"]:
        n = e["node"]
        if n.get("completedAt"):
            continue
        total_shopify += 1
        cli = n.get("customer") or {}
        email = ((cli.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip().lower()
        sa, ba = n.get("shippingAddress") or {}, n.get("billingAddress") or {}
        candidatos = [(sa.get("phone"), sa.get("countryCodeV2")), (ba.get("phone"), ba.get("countryCodeV2") or sa.get("countryCodeV2")),
                      ((cli.get("defaultPhoneNumber") or {}).get("phoneNumber"), sa.get("countryCodeV2"))]
        if not any((c[0] or "").strip() for c in candidatos):
            sin_tel += 1
            continue
        tel = None
        for crudo, pais in candidatos:
            tel = e164(crudo, pais)
            if tel:
                break
        if not tel:
            sin_normalizar += 1
            continue
        if email and email not in abandonos:        # vienen del mas reciente al mas antiguo
            abandonos[email] = (tel, n["createdAt"])
    if not ac["pageInfo"]["hasNextPage"]:
        break
    cursor = ac["pageInfo"]["endCursor"]

print(f"Shopify: {total_shopify} checkouts sin completar | {sin_tel} sin telefono | {sin_normalizar} con telefono ilegible | {len(abandonos)} personas con telefono valido")

CAMPOS = "additional-fields[profile]=subscriptions&fields[profile]=email,phone_number,subscriptions,properties&page[size]=100"


def perfiles_por(campo, valores):
    out = {}
    valores = list(valores)
    for i in range(0, len(valores), 50):
        lote = valores[i:i + 50]
        filtro = "any(%s,[%s])" % (campo, ",".join(json.dumps(v) for v in lote))
        url = f"{KLAVIYO}/profiles?filter={urllib.parse.quote(filtro, safe='(),[]')}&{CAMPOS}"
        while url:
            code, d = kl("GET", url)
            if "data" not in d:
                print(f"  aviso: lectura de perfiles HTTP {code}: {limpia(d.get('errors'))[:160]}")
                break
            for p in d["data"]:
                out[p["id"]] = p["attributes"]
            url = (d.get("links") or {}).get("next")
    return out


def estado(attrs, canal_):
    s = (attrs.get("subscriptions") or {}).get(canal_) or {}
    return (s.get("marketing") or {}).get("consent") or "NEVER_SUBSCRIBED"


# 2. Klaviyo: perfiles de esos emails
por_email = {}
for pid, a in perfiles_por("email", abandonos.keys()).items():
    em = (a.get("email") or "").strip().lower()
    if em:
        por_email[em] = (pid, a)

poner_tel = []          # (id, telefono)
altas = {}              # telefono -> (canal, id de perfil o None)
cuenta = collections.Counter()
for email, (tel, creado) in abandonos.items():
    if email not in por_email:
        cuenta["abandono: sin perfil en Klaviyo"] += 1
        continue
    pid, a = por_email[email]
    actual = a.get("phone_number")
    if actual:
        cuenta["abandono: el perfil ya tenia telefono"] += 1
        tel = actual
    else:
        cuenta["abandono: perfil SIN telefono (se lo pondria)"] += 1
        poner_tel.append((pid, tel))
    c = canal(tel)
    est = estado(a, c)
    cuenta[f"abandono: {c} {est}"] += 1
    if est != "NEVER_SUBSCRIBED":
        continue
    if c == "sms" and (not CON_SMS or minutos_desde(creado) > SMS_MAX_MIN):
        cuenta["abandono: sms que no se toca (apagado o checkout antiguo)"] += 1
        continue
    altas[tel] = (c, pid)

# 3. Klaviyo: perfiles con Checkout Started reciente que ya tienen telefono (compradores, clientes que repiten)
ids = set()
ultimo = {}             # perfil -> fecha de su checkout mas reciente
url = (f'{KLAVIYO}/events?filter=and(equals(metric_id,"{METRIC_CHECKOUT}"),greater-or-equal(datetime,{desde_iso}))'
       f'&sort=-datetime&page[size]=200&fields[event]=datetime')
while url:
    code, d = kl("GET", url)
    if "data" not in d:
        print(f"  aviso: lectura de eventos HTTP {code}")
        break
    for e in d["data"]:
        pid = (((e.get("relationships") or {}).get("profile") or {}).get("data") or {}).get("id")
        if pid:
            ids.add(pid)
            ultimo.setdefault(pid, e["attributes"]["datetime"])     # vienen del mas reciente al mas antiguo
    url = (d.get("links") or {}).get("next")
ya_vistos = {pid for pid, _ in por_email.values()}
for pid, a in perfiles_por("id", ids - ya_vistos).items():
    tel = a.get("phone_number")
    if not tel:
        cuenta["checkout en Klaviyo: perfil sin telefono"] += 1
        continue
    c = canal(tel)
    est = estado(a, c)
    cuenta[f"checkout en Klaviyo con telefono: {c} {est}"] += 1
    if est != "NEVER_SUBSCRIBED":
        continue
    if c == "sms" and (not CON_SMS or minutos_desde(ultimo[pid]) > SMS_MAX_MIN):
        continue
    altas.setdefault(tel, (c, pid))

print(f"Klaviyo: {len(ids)} perfiles con Checkout Started en la ventana")
for k in sorted(cuenta):
    print(f"   {cuenta[k]:4d}  {k}")
n_wa = sum(1 for c, _ in altas.values() if c == "whatsapp")
n_sms = sum(1 for c, _ in altas.values() if c == "sms")
print(f"RESULTADO: poner telefono a {len(poner_tel)} perfiles | altas de WhatsApp {n_wa} | altas de SMS {n_sms}")

REGISTRO = arg("--registro")           # copia local de lo que se va a tocar (lleva datos personales: no subir)
if REGISTRO:
    json.dump({"fecha": ahora.isoformat(), "aplicado": APLICAR, "poner_telefono": poner_tel,
               "altas": [{"telefono": t, "canal": c, "perfil": pid} for t, (c, pid) in altas.items()]},
              open(REGISTRO, "w"), indent=1)
    os.chmod(REGISTRO, 0o600)

if not APLICAR:
    print("PRUEBA: no se ha escrito nada. Con --apply se ejecuta.")
    sys.exit(0)

# ---------------------------------------------------------------- escritura

if len(altas) > TOPE:
    sys.exit(f"ALTO: {len(altas)} altas superan el tope de {TOPE}. Revisa y sube --max si es correcto.")

ok_tel = dup = err_tel = 0
telefonos_fallidos = set()
for pid, tel in poner_tel:
    code, d = kl("PATCH", f"profiles/{pid}", {"data": {"type": "profile", "id": pid, "attributes": {"phone_number": tel}}})
    if code in (200, 201):
        ok_tel += 1
    elif code == 409:
        dup += 1                        # ese telefono ya es de otro perfil: el alta ira a ese otro perfil
    else:
        err_tel += 1
        telefonos_fallidos.add(tel)
print(f"telefonos puestos: {ok_tel} | ya eran de otro perfil: {dup} | rechazados por Klaviyo: {err_tel}")

hoy = ahora.strftime("%Y-%m-%d")
for c, fuente in (("whatsapp", "Checkout WA auto-consent"), ("sms", "Checkout SMS auto-consent")):
    lote_total = [(t, pid) for t, (cc, pid) in altas.items() if cc == c and t not in telefonos_fallidos]
    hechos = 0
    for i in range(0, len(lote_total), 100):
        lote = lote_total[i:i + 100]
        body = {"data": {"type": "profile-subscription-bulk-create-job", "attributes": {
            "custom_source": fuente,
            "profiles": {"data": [{"type": "profile", "attributes": {
                "phone_number": t, "subscriptions": {c: {"marketing": {"consent": "SUBSCRIBED"}}}}} for t, _ in lote]}},
            "relationships": {"list": {"data": {"type": "list", "id": LIST_ID}}}}}
        code, d = kl("POST", "profile-subscription-bulk-create-jobs", body)
        if code in (200, 201, 202):
            hechos += len(lote)
            if c == "sms":              # una sola peticion de confirmacion por persona
                for _, pid in lote:
                    if pid:
                        kl("PATCH", f"profiles/{pid}", {"data": {"type": "profile", "id": pid,
                                                                  "attributes": {"properties": {MARCA_SMS: hoy}}}})
        else:
            print(f"  ERROR alta {c} (HTTP {code}): {limpia(d.get('errors'))[:200]}")
    print(f"altas de {c} enviadas a Klaviyo: {hechos}")
