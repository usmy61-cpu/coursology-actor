"""
scraper.py — Core scraping logic ported from Selenium → Playwright (async).

All driver.execute_script(js, el)  →  await page.evaluate(js, el)
All driver.find_element(...)       →  page.locator(...) / page.query_selector(...)
All time.sleep(x)                  →  await asyncio.sleep(x)
Audio saving                       →  storage.save_audio()
"""
from __future__ import annotations

import asyncio
import base64
import re
import random
from typing import Any

from playwright.async_api import Page, ElementHandle

from storage import save_audio, save_question, save_state

# ── UI noise to strip from text ───────────────────────────────────────────────
UI_NOISE = [
    "Medical Library", "My Notebook", "Flashcards", "Feedback", "End Review",
    "Calculator", "Lab Values", "Mark", "Previous", "Next", "Shortcuts",
    "Full Screen", "Marker", "Notes", "Settings", "Consult AI Tutor",
    "REVIEW -", "Block Time Elapsed", "Version", "Test ID",
    "Item ", "Question Id", "Answered Correctly", "Time Spent",
    "Incorrect", "Correct answer",
]


def strip_ui_noise(text: str) -> str:
    if not text:
        return text
    lines = text.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(noise in stripped for noise in UI_NOISE):
            continue
        if re.match(r'^(Item\s+)?\d+\s*(of\s+\d+)?$', stripped):
            continue
        clean.append(stripped)
    return "\n".join(clean).strip()


# ── JS walker — shared between question and explanation ────────────────────────
_QUESTION_JS_WALKER = """
(el) => {
var KEEP_TAGS = new Set(['P','STRONG','EM','B','I','U','S','MARK','SUP','SUB',
    'TABLE','THEAD','TBODY','TR','TD','TH',
    'UL','OL','LI','BR','H1','H2','H3','H4','H5','H6','AUDIO','VIDEO','SOURCE']);
var SKIP_TAGS = new Set(['SVG','NAV','INPUT','SCRIPT','STYLE',
    'PATH','CIRCLE','RECT','POLYGON']);

function shouldStop(node) {
    if (!node.getAttribute) return false;
    if (node.getAttribute('aria-label') === 'Question choices') return true;
    var cls = (node.className || '').toString();
    if (cls.indexOf('border-red-500') !== -1) return true;
    if (cls.indexOf('border-lime-500') !== -1) return true;
    if (cls.indexOf('gap-2.5') !== -1 && cls.indexOf('flex-col') !== -1) return true;
    return false;
}

function getHtml(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    var tag = node.tagName;
    if (shouldStop(node)) return '';
    if (SKIP_TAGS.has(tag)) return '';
    if (tag === 'BUTTON' && (node.id || '').startsWith('exhibit-')) {
        var nm = node.textContent.trim();
        return '<button class="exhibit-btn" data-exhibit="' + nm + '">' + nm + '</button>';
    }
    if (tag === 'IMG') {
        var src = node.getAttribute('src');
        if (!src || src.startsWith('data:')) return '';
        return '<img src="' + src + '" alt="' + (node.getAttribute('alt')||'') + '" style="max-width:100%;">';
    }
    if (tag === 'AUDIO' || tag === 'VIDEO') {
        var src = node.getAttribute('src');
        if (!src) { var s = node.querySelector('source'); if (s) src = s.getAttribute('src'); }
        if (!src) return '';
        var t = tag.toLowerCase();
        return src.startsWith('data:') ? '<' + t + ' controls data-src="base64"></' + t + '>'
                                       : '<' + t + ' controls src="' + src + '"></' + t + '>';
    }
    var inner = Array.from(node.childNodes).map(getHtml).join('');
    var hasImage = node.querySelector && node.querySelector('img');
    if (!inner.trim() && !hasImage) return '';
    if (tag==='SPAN'||tag==='DIV'||tag==='SECTION'||tag==='BUTTON') return inner;
    var attrs = '';
    if (tag==='TD'||tag==='TH') {
        var cs=node.getAttribute('colspan'),rs=node.getAttribute('rowspan');
        if(cs) attrs+=' colspan="'+cs+'"'; if(rs) attrs+=' rowspan="'+rs+'"';
    }
    if (KEEP_TAGS.has(tag)) { var t=tag.toLowerCase(); return '<'+t+attrs+'>'+inner+'</'+t+'>'; }
    return inner;
}

function getText(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    var tag = node.tagName;
    if (shouldStop(node)) return '';
    if (SKIP_TAGS.has(tag)) return '';
    if (tag==='BUTTON') { return (node.id||'').startsWith('exhibit-') ? '[EXHIBIT: '+node.textContent.trim()+']' : ''; }
    if (tag==='AUDIO') return '[AUDIO]';
    if (tag==='VIDEO') return '[VIDEO]';
    if (tag==='TABLE') {
        return Array.from(node.querySelectorAll('tr')).map(function(r){
            return Array.from(r.children).filter(function(c){return c.tagName==='TD'||c.tagName==='TH';})
                .map(function(c){return (c.innerText||c.textContent).trim();}).join('\t');
        }).join('\n');
    }
    return Array.from(node.childNodes).map(getText).join('');
}

return { html: getHtml(el).trim(), text: getText(el).trim() };
}
"""

_EXPLANATION_HTML_JS = """
(el) => {
var KEEP_TAGS = new Set(['P','STRONG','EM','B','I','U','S','MARK',
    'TABLE','THEAD','TBODY','TR','TD','TH','UL','OL','LI','BR',
    'H1','H2','H3','H4','H5','H6','AUDIO','VIDEO','SOURCE']);
var SKIP_TAGS = new Set(['SVG','NAV','INPUT','SCRIPT','STYLE','PATH','CIRCLE','RECT','POLYGON']);
var UI_HINTS  = ['fixed','z-[','cursor-','radix-','navbar','sidebar','scrollbar',
    'group/draggable','group/resizable','group/triggerable','data-radix'];

function isUIChrome(node) {
    var cls=(node.className||'').toString();
    return UI_HINTS.some(function(h){return cls.indexOf(h)!==-1;});
}

function buildHtml(node) {
    if (node.nodeType===3) return node.textContent;
    if (node.nodeType!==1) return '';
    var tag=node.tagName;
    if (SKIP_TAGS.has(tag)) return '';
    if (tag==='BUTTON'&&(node.id||'').startsWith('exhibit-'))
        return '<button class="exhibit-btn" data-exhibit="'+node.textContent.trim()+'">'+node.textContent.trim()+'</button>';
    if (tag==='IMG') {
        var src=node.getAttribute('src');
        if (!src||src.startsWith('data:')) return '';
        return '<img src="'+src+'" alt="'+(node.getAttribute('alt')||'')+'" style="max-width:100%;">';
    }
    if (tag==='AUDIO'||tag==='VIDEO') {
        var src=node.getAttribute('src');
        if (!src){var s=node.querySelector('source');if(s)src=s.getAttribute('src');}
        if (!src) return '';
        var t=tag.toLowerCase();
        return src.startsWith('data:')?'<'+t+' controls data-src="base64"></'+t+'>'
                                      :'<'+t+' controls src="'+src+'"></'+t+'>';
    }
    var inner=Array.from(node.childNodes).map(buildHtml).join('');
    var hasImage=node.querySelector&&node.querySelector('img');
    if(!inner.trim()&&!hasImage) return '';
    if(tag==='DIV'||tag==='SECTION'||tag==='SPAN'||tag==='BUTTON'||tag==='NAV') return inner;
    var attrs='';
    if(tag==='TD'||tag==='TH'){
        var cs=node.getAttribute('colspan'),rs=node.getAttribute('rowspan');
        if(cs) attrs+=' colspan="'+cs+'"'; if(rs) attrs+=' rowspan="'+rs+'"';
    }
    if(KEEP_TAGS.has(tag)){var t=tag.toLowerCase();return '<'+t+attrs+'>'+inner+'</'+t+'>';}
    return inner;
}
return buildHtml(el).trim();
}
"""

_EXHIBIT_MEDIA_JS = """
([panelId, modalRoot]) => {
var root = modalRoot || document;
var CDN = 'cdn.coursology-qbank.com';

function isVisible(el) {
    if (!el) return false;
    var st=window.getComputedStyle(el);
    if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity)<0.1) return false;
    var r=el.getBoundingClientRect(); return r.width>5&&r.height>5;
}
function findIn(r) {
    if (!r) return null;
    var v=r.querySelector('video');
    if(v){var vs=v.getAttribute('src');if(vs&&vs.indexOf(CDN)!==-1)return{url:vs,type:'video'};
          var s=v.querySelector('source');if(s&&s.getAttribute('src')&&s.getAttribute('src').indexOf(CDN)!==-1)return{url:s.getAttribute('src'),type:'video'};}
    var a=r.querySelector('audio');
    if(a){var as=a.getAttribute('src');if(as&&as.indexOf(CDN)!==-1)return{url:as,type:'audio'};
          var s=a.querySelector('source');if(s&&s.getAttribute('src')&&s.getAttribute('src').indexOf(CDN)!==-1)return{url:s.getAttribute('src'),type:'audio'};}
    var imgs=Array.from(r.querySelectorAll('img'));
    for(var img of imgs){var src=img.getAttribute('src')||'';if(src.indexOf(CDN)!==-1&&isVisible(img))return{url:src,type:'image'};}
    return null;
}

if(panelId){var p=document.getElementById(panelId);var res=findIn(p);if(res)return res;}
var active=root.querySelector('[role="tabpanel"][data-state="active"],[role="tabpanel"]:not([hidden])');
var r2=findIn(active);if(r2)return r2;
var allImgs=Array.from(root.querySelectorAll('img'));
for(var img of allImgs){if(isVisible(img)){var s=img.getAttribute('src')||img.getAttribute('data-src')||'';if(s.indexOf(CDN)!==-1)return{url:s,type:'image'};}}
return null;
}
"""

_MODAL_CONTENT_JS = """
([panelId, modalEl]) => {
var root = modalEl;
if (panelId) { var p=document.getElementById(panelId); if(p) root=p; }
if (!root) { root=document.querySelector('[role="dialog"]')||document.querySelector('.fixed.inset-0'); }
if (!root) return {text:'',html:''};

var KEEP_TAGS=new Set(['P','STRONG','EM','B','I','U','S','MARK','SUP','SUB',
    'TABLE','THEAD','TBODY','TR','TD','TH','UL','OL','LI','BR','H1','H2','H3','H4','H5','H6','AUDIO','VIDEO','SOURCE']);
var SKIP_TAGS=new Set(['SVG','NAV','INPUT','SCRIPT','STYLE','PATH','CIRCLE','RECT','POLYGON']);

function isVisible(el){
    if(el.nodeType===3)return true;if(el.nodeType!==1)return false;
    var st=window.getComputedStyle(el);
    return st.display!=='none'&&st.visibility!=='hidden'&&parseFloat(st.opacity)>=0.05;
}
function getHtml(node){
    if(node.nodeType===3)return node.textContent;
    if(node.nodeType!==1)return '';
    var tag=node.tagName;
    if(SKIP_TAGS.has(tag))return '';
    if(tag==='BUTTON'||(node.getAttribute&&node.getAttribute('role')==='tablist'))return '';
    if(!isVisible(node))return '';
    if(tag==='IMG'){var src=node.getAttribute('src');if(!src||src.startsWith('data:'))return '';return '<img src="'+src+'" alt="'+(node.getAttribute('alt')||'')+'" style="max-width:100%;">';}
    var inner=Array.from(node.childNodes).map(getHtml).join('');
    if(!inner.trim()&&tag!=='BR')return '';
    if(tag==='DIV'||tag==='SECTION'||tag==='SPAN')return inner;
    if(KEEP_TAGS.has(tag)){var t=tag.toLowerCase();var attrs='';if(tag==='TD'||tag==='TH'){var cs=node.getAttribute('colspan'),rs=node.getAttribute('rowspan');if(cs)attrs+=' colspan="'+cs+'"';if(rs)attrs+=' rowspan="'+rs+'"';}return '<'+t+attrs+'>'+inner+'</'+t+'>';}
    return inner;
}
function getText(node){
    if(node.nodeType===3)return node.textContent;
    if(node.nodeType!==1)return '';
    var tag=node.tagName;
    if(SKIP_TAGS.has(tag))return '';
    if(tag==='BUTTON'||(node.getAttribute&&node.getAttribute('role')==='tablist'))return '';
    if(tag==='AUDIO')return '[AUDIO]';if(tag==='VIDEO')return '[VIDEO]';
    if(!isVisible(node))return '';
    if(tag==='TABLE')return Array.from(node.querySelectorAll('tr')).map(function(r){return Array.from(r.children).filter(function(c){return c.tagName==='TD'||c.tagName==='TH';}).map(function(c){return(c.innerText||c.textContent).trim();}).join('\t');}).join('\n');
    return Array.from(node.childNodes).map(getText).join('');
}
var result={html:getHtml(root).trim(),text:getText(root).trim()};
if(!result.html){var table=root.querySelector('table');if(table){result.html=getHtml(table).trim();result.text=getText(table).trim();}}
return result;
}
"""

_CLOSE_MODALS_JS = """
() => {
var cb=document.querySelectorAll('button.bg-red-500,.bg-red-500 button,[class*="close"],[aria-label*="Close"]');
cb.forEach(function(b){try{if(b.offsetWidth>0)b.click();}catch(e){}});
var dialogs=document.querySelectorAll('[role="dialog"],[data-state="open"]');
dialogs.forEach(function(d){d.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
return cb.length>0||dialogs.length>0;
}
"""

_AUDIO_SCRAPE_JS = """
() => {
var results=[];
var audios=document.querySelectorAll('audio');
for(var i=0;i<audios.length;i++){
    var a=audios[i];
    var src=a.getAttribute('src')||'';
    var mimeType=null;
    if(!src){
        var sources=a.querySelectorAll('source');
        for(var j=0;j<sources.length;j++){var s=sources[j];var ss=s.getAttribute('src')||'';if(ss){src=ss;mimeType=s.getAttribute('type')||null;break;}}
    }
    if(!src)continue;
    var location='page';
    var el=a;
    while(el){
        if(el.id==='question-explanation'||(el.className&&el.className.toString().indexOf('explanation')!==-1)){location='explanation';break;}
        if(el.id==='question-text'){location='question';break;}
        if(el.getAttribute&&el.getAttribute('aria-label')==='Question choices'){location='choice';break;}
        el=el.parentElement;
    }
    results.push({src:src,mimeType:mimeType,location:location});
}
return results;
}
"""


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

async def _get_body_text(page: Page) -> str:
    try:
        return await page.inner_text("body") or ""
    except Exception:
        return ""


async def get_total(page: Page) -> int | None:
    body = await _get_body_text(page)
    m = re.search(r'[Ii]tem\s+\d+\s+of\s+(\d+)', body)
    if m:
        total = int(m.group(1))
        print(f"[+] Detected {total} total questions.")
        return total
    return None


async def get_qid(page: Page) -> str | None:
    body = await _get_body_text(page)
    m = re.search(r'[Qq]uestion\s+[Ii]d[:\s]+(\d+)', body)
    return m.group(1) if m else None


async def get_question(page: Page) -> dict:
    """Returns {text, html}."""
    try:
        containers = await page.query_selector_all("div#question-text")
        for container in containers:
            try:
                in_expl = await container.query_selector_all(
                    "xpath=./ancestor::*[@id='question-explanation']"
                )
                if in_expl:
                    continue
                result = await page.evaluate(_QUESTION_JS_WALKER, container)
                if result:
                    text = (result.get("text") or "").strip()
                    html = (result.get("html") or "").strip()
                    if len(text) > 80:
                        return {"text": text, "html": html}
            except Exception:
                pass

        # Fallback: single <p>
        paras = await page.query_selector_all("p.mb-4.text-start")
        for p in paras:
            try:
                in_expl = await p.query_selector_all(
                    "xpath=./ancestor::*[@id='question-explanation' or contains(@class,'explanation')]"
                )
                if in_expl:
                    continue
                t = (await p.inner_text() or "").strip()
                if t and len(t) > 80:
                    h = await p.get_attribute("outerHTML") or ""
                    return {"text": t, "html": h}
            except Exception:
                pass
    except Exception as e:
        print(f"    [!] get_question error: {e}")
    return {"text": None, "html": None}


async def click_answer(page: Page) -> bool:
    """Click a random un-selected answer choice. Returns True if clicked."""
    try:
        container = None
        for sel in [
            "[aria-label='Question choices']",
            ".choices-container",
            ".gap-2\\.5.py-2.flex.flex-col",
        ]:
            container = await page.query_selector(sel)
            if container:
                break

        if not container:
            return False

        # Check if already answered
        expl = await page.query_selector("div#question-explanation")
        if expl:
            return False

        buttons = await container.query_selector_all("button, div.group\\/choice")
        visible = [b for b in buttons if await b.is_visible()]
        if not visible:
            return False

        target = random.choice(visible)
        await target.scroll_into_view_if_needed()
        await asyncio.sleep(0.2)
        await target.click()
        return True
    except Exception as e:
        print(f"    [!] click_answer error: {e}")
        return False


async def wait_for_explanation(page: Page, timeout: float = 8.0) -> bool:
    """Wait for the explanation panel to appear after clicking an answer."""
    try:
        await page.wait_for_selector(
            "div#question-explanation, .explanation-box, .discussion-section",
            timeout=timeout * 1000,
        )
        return True
    except Exception:
        return False


async def get_choices(page: Page) -> tuple[list, str | None]:
    choices: list[dict] = []
    choices_header: str | None = None

    try:
        container = None
        for sel in ["[aria-label='Question choices']", ".choices-container"]:
            container = await page.query_selector(sel)
            if container:
                break
        if not container:
            return choices, choices_header

        # Optional shared table header
        try:
            header_el = await container.query_selector("thead th, .choices-header, th")
            if header_el:
                choices_header = (await header_el.inner_text() or "").strip() or None
        except Exception:
            pass

        rows = await container.query_selector_all(
            "button[id^='choice'], div.group\\/choice, li.choice, tr.choice-row, button"
        )

        for row in rows:
            try:
                if not await row.is_visible():
                    continue
                full_text = (await row.inner_text() or "").strip()
                if not full_text:
                    continue

                # Label extraction: leading "A.", "A)", or "A "
                label = None
                m = re.match(r'^([A-E])[\.\)]\s*', full_text)
                if m:
                    label = m.group(1)
                    full_text = full_text[m.end():]

                # Strip percentage suffix  "61%"
                percentage: int | None = None
                pm = re.search(r'\s+(\d{1,3})%\s*$', full_text)
                if pm:
                    percentage = int(pm.group(1))
                    full_text = full_text[:pm.start()].strip()

                clean_text = strip_ui_noise(full_text).strip()

                # Status
                status: str | None = None
                cls = await row.get_attribute("class") or ""
                aria = await row.get_attribute("aria-pressed") or ""
                if "lime" in cls or "correct" in cls.lower() or "border-lime" in cls:
                    status = "correct"
                elif "selected" in cls or "ring" in cls or aria == "true":
                    status = "selected"
                else:
                    # Fallback: check inner SVG class
                    try:
                        svgs = await row.query_selector_all("svg")
                        for svg in svgs:
                            sc = await svg.get_attribute("class") or ""
                            if "fa-check" in sc or "lime" in sc:
                                status = "correct"
                                break
                    except Exception:
                        pass

                image: str | None = None
                try:
                    img_el = await row.query_selector("img")
                    if img_el:
                        image = await img_el.get_attribute("src")
                except Exception:
                    pass

                if label or clean_text or image:
                    choices.append({
                        "label":      label,
                        "text":       clean_text,
                        "percentage": percentage,
                        "image":      image,
                        "status":     status,
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"    [!] get_choices error: {e}")

    return choices, choices_header


def get_correct_info(choices: list) -> tuple[str | None, str | None]:
    for c in choices:
        if c.get("status") == "correct":
            return c.get("label"), c.get("text")
    return None, None


async def get_explanation(page: Page) -> dict:
    try:
        elem = None
        for sel in ["div#question-explanation", ".explanation-box", ".discussion-section"]:
            elem = await page.query_selector(sel)
            if elem:
                break
        if not elem:
            return {"text": None, "html": None}

        raw_text = (await elem.inner_text() or "").strip()
        clean_text = strip_ui_noise(raw_text)
        clean_text = re.sub(r'\n?--- EXHIBIT:.*?---\n?', '', clean_text)
        clean_text = re.sub(r'\[IMAGE:[^\]]+\]', '', clean_text).strip()

        clean_html = await page.evaluate(_EXPLANATION_HTML_JS, elem) or ""

        if len(clean_text) > 10:
            return {"text": clean_text, "html": clean_html}
    except Exception as e:
        print(f"    [!] get_explanation error: {e}")
    return {"text": None, "html": None}


async def get_metadata(page: Page) -> dict:
    meta: dict = {}
    try:
        blocks = await page.query_selector_all("div.flex-col.justify-center.items-start")
        for block in blocks:
            try:
                ps = await block.query_selector_all("p")
                if len(ps) == 2:
                    t1 = (await ps[0].inner_text() or "").strip()
                    t2 = (await ps[1].inner_text() or "").strip()
                    if t2.lower() in ["subject", "system", "topic", "category"]:
                        meta[t2.lower()] = t1
                    elif t1.lower() in ["subject", "system", "topic", "category"]:
                        meta[t1.lower()] = t2
            except Exception:
                continue
    except Exception:
        pass
    return meta


async def get_references(page: Page) -> list:
    refs: list[dict] = []
    try:
        # Strategy 1: find "References" label then siblings
        label_el = await page.query_selector(
            "xpath=//span[contains(text(),'References')] | //p[contains(text(),'References')]"
        )
        if label_el:
            parent = await label_el.query_selector("xpath=./parent::*")
            if parent:
                links = await parent.query_selector_all("a")
                for a in links:
                    title = (await a.inner_text() or "").strip()
                    url = await a.get_attribute("href")
                    if title and url:
                        refs.append({"title": title, "url": url})
            if refs:
                return _dedup_refs(refs)

        # Strategy 2: explanation-area links
        expl = await page.query_selector("div#question-explanation")
        if expl:
            all_links = await expl.query_selector_all("a")
            for a in all_links:
                url = await a.get_attribute("href") or ""
                title = (await a.inner_text() or "").strip()
                if not title:
                    sp = await a.query_selector("span")
                    if sp:
                        title = (await sp.inner_text() or "").strip()
                if url and any(kw in url.lower() for kw in ["pubmed", "ncbi", "doi", "medscape", "uptodate"]):
                    refs.append({"title": title or "Source Link", "url": url})
    except Exception as e:
        print(f"    [!] get_references error: {e}")
    return _dedup_refs(refs)


def _dedup_refs(refs: list) -> list:
    seen: set = set()
    out: list = []
    for r in refs:
        key = (r["title"], r["url"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── Exhibits ──────────────────────────────────────────────────────────────────

async def _close_modals(page: Page) -> None:
    try:
        await page.evaluate(_CLOSE_MODALS_JS)
        await asyncio.sleep(0.6)
    except Exception:
        pass


async def _wait_for_exhibit_modal(page: Page, name: str, max_attempts: int = 4) -> Any:
    for attempt in range(max_attempts):
        try:
            for sel in [
                "[role='dialog']",
                "[data-state='open']",
                "div[class*='Modal']",
                ".fixed.inset-0 > div",
            ]:
                els = await page.query_selector_all(sel)
                for el in els:
                    if await el.is_visible():
                        text = (await el.inner_text() or "").strip()
                        if len(text) > 10:
                            return el
                for el in els:
                    if await el.is_visible():
                        return el
        except Exception:
            pass

        found_media = await page.evaluate(
            "() => !!document.querySelector('video source[src*=\"cdn.coursology\"]') "
            "|| !!document.querySelector('img[src*=\"cdn.coursology\"]')"
        )
        if found_media:
            for sel in ["[role='dialog']", "div[class*='Modal']"]:
                els = await page.query_selector_all(sel)
                for e in els:
                    if await e.is_visible():
                        return e
            return None

        print(f"      [...] Waiting for exhibit '{name}' (attempt {attempt+1}/{max_attempts})…")
        await asyncio.sleep(1.0)
    return None


async def scrape_all_exhibits(page: Page) -> list:
    exhibits: list[dict] = []
    seen_names: set = set()

    try:
        btns = await page.query_selector_all("button[id^='exhibit-']")
        if not btns:
            return exhibits
        print(f"    [+] Found {len(btns)} exhibit button(s)")

        for i, btn in enumerate(btns):
            name = "unknown"
            modal_el = None
            try:
                await _close_modals(page)
                await asyncio.sleep(0.3)

                if not await btn.is_visible():
                    continue

                name = (await btn.inner_text() or "").strip() or await btn.get_attribute("id") or f"exhibit_{i}"
                if name in seen_names:
                    continue
                seen_names.add(name)

                # Location
                location = "question"
                try:
                    in_expl = await btn.query_selector_all("xpath=./ancestor::*[@id='question-explanation']")
                    if in_expl:
                        location = "explanation"
                except Exception:
                    pass

                await btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)
                await page.evaluate("(el) => el.click()", btn)

                try:
                    modal_el = await _wait_for_exhibit_modal(page, name)
                    await asyncio.sleep(0.5)

                    tabs = await page.query_selector_all("[role='tablist'] button")
                    if tabs and len(tabs) > 1:
                        print(f"      [...] Multi-tab exhibit ({len(tabs)} tabs)")
                        for idx, tab in enumerate(tabs):
                            try:
                                t_name = (await tab.inner_text() or "").strip() or f"Tab {idx+1}"
                                panel_id = await tab.get_attribute("aria-controls")
                                await tab.scroll_into_view_if_needed()
                                await asyncio.sleep(0.2)
                                await page.evaluate("(el) => el.click()", tab)
                                await asyncio.sleep(1.5)

                                media = await page.evaluate(_EXHIBIT_MEDIA_JS, [panel_id, modal_el])
                                m_url = media.get("url") if media else None
                                m_type = media.get("type") if media else None

                                content = await page.evaluate(_MODAL_CONTENT_JS, [panel_id, modal_el])
                                exhibits.append({
                                    "name":       f"{name} ({t_name})",
                                    "location":   location,
                                    "media_url":  m_url,
                                    "media_type": m_type,
                                    "text":       strip_ui_noise(content.get("text") or ""),
                                    "html":       content.get("html") or "",
                                })
                            except Exception as te:
                                print(f"      [!] Tab {idx} error: {te}")
                    else:
                        media = await page.evaluate(_EXHIBIT_MEDIA_JS, [None, modal_el])
                        m_url = media.get("url") if media else None
                        m_type = media.get("type") if media else None
                        content = await page.evaluate(_MODAL_CONTENT_JS, [None, modal_el])
                        exhibits.append({
                            "name":       name,
                            "location":   location,
                            "media_url":  m_url,
                            "media_type": m_type,
                            "text":       strip_ui_noise(content.get("text") or ""),
                            "html":       content.get("html") or "",
                        })
                    print(f"      [✓] Exhibit '{name}' done")
                except Exception as ei:
                    print(f"      [!] Exhibit '{name}' error: {ei}")
                finally:
                    await _close_modals(page)
                    await asyncio.sleep(0.5)

            except Exception as ex:
                print(f"    [!] Exhibit button {i} ('{name}') error: {ex}")

    except Exception as e:
        print(f"    [!] scrape_all_exhibits error: {e}")
    return exhibits


# ── Audio ─────────────────────────────────────────────────────────────────────

async def scrape_audio(page: Page, question_number: int, save: bool = True) -> list:
    audio_list: list[dict] = []

    EXT_MAP = {
        "audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/m4a": ".m4a",
        "audio/aac": ".aac", "audio/flac": ".flac", "audio/webm": ".webm",
    }

    try:
        raw_results = await page.evaluate(_AUDIO_SCRAPE_JS)
        if not raw_results:
            return audio_list

        for idx, item in enumerate(raw_results):
            src = item.get("src", "")
            mime_type = item.get("mimeType")
            location = item.get("location", "page")
            if not src:
                continue

            if src.startswith("data:"):
                try:
                    header, b64data = src.split(",", 1)
                    if not mime_type:
                        m = re.match(r'data:([^;]+)', header)
                        if m:
                            mime_type = m.group(1)
                    ext = EXT_MAP.get(mime_type or "", ".mp3")
                    filename = f"q{question_number}_audio_{idx + 1}{ext}"
                    audio_bytes = base64.b64decode(b64data)

                    if save:
                        kv_url = await save_audio(filename, audio_bytes, mime_type or "audio/mpeg")
                        print(f"    [♫] Saved audio to KV store: {filename} ({len(audio_bytes):,} bytes)")
                        audio_list.append({
                            "location":  location,
                            "src_type":  "base64",
                            "file_path": kv_url,
                            "url":       None,
                            "mime_type": mime_type,
                        })
                    else:
                        audio_list.append({
                            "location":  location,
                            "src_type":  "base64",
                            "file_path": None,
                            "url":       None,
                            "mime_type": mime_type,
                        })
                except Exception as e:
                    print(f"    [!] Audio decode error: {e}")
            else:
                print(f"    [♫] Audio URL: {src[:80]}…")
                audio_list.append({
                    "location":  location,
                    "src_type":  "url",
                    "file_path": None,
                    "url":       src,
                    "mime_type": mime_type,
                })

    except Exception as e:
        print(f"    [!] scrape_audio error: {e}")
    return audio_list


async def click_next(page: Page) -> bool:
    await _close_modals(page)
    for xpath in [
        "//button[contains(text(),'Next')]",
        "//button[normalize-space()='Next']",
        "//button[contains(@aria-label,'Next')]",
        "//a[contains(text(),'Next')]",
    ]:
        try:
            btn = await page.query_selector(f"xpath={xpath}")
            if btn and await btn.is_visible() and await btn.is_enabled():
                await btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                await btn.click()
                return True
        except Exception:
            continue
    return False


# ── Main scrape loop ──────────────────────────────────────────────────────────

async def scrape(
    page: Page,
    max_questions: int = 0,
    start_from: int = 1,
    delay_min_ms: int = 1200,
    delay_max_ms: int = 2500,
    save_audio_files: bool = True,
) -> None:
    """
    Main scrape loop. Pushes each question directly to Apify Dataset.
    Saves progress state after every question so runs can be resumed.
    """
    total = await get_total(page)
    effective_max = max_questions if max_questions > 0 else (total or 9999)

    print(f"[*] Scraping up to {effective_max} questions (start_from={start_from})…\n")

    # Skip ahead if resuming
    if start_from > 1:
        print(f"[*] Fast-forwarding to question {start_from}…")
        for skip in range(start_from - 1):
            await asyncio.sleep(0.5)
            if not await click_next(page):
                print(f"[!] Could not skip to question {start_from}, stopping early.")
                return

    n = start_from - 1
    while True:
        n += 1
        delay_s = random.randint(delay_min_ms, delay_max_ms) / 1000
        await asyncio.sleep(delay_s)

        qid = await get_qid(page)
        q_data = await get_question(page)

        if not q_data["text"]:
            print(f"[!] No question text at item {n}. Stopping.")
            break

        lbl = f"[{n}/{total}]" if total else f"[{n}]"
        print(f"  {lbl} QID:{qid or '?'} — scraping…")

        # Click answer, wait for explanation
        clicked = await click_answer(page)
        if clicked:
            found = await wait_for_explanation(page)
            if not found:
                print(f"    [!] Explanation did not appear for Q{n}")
        else:
            await asyncio.sleep(1.0)

        # Gather all data
        exhibits   = await scrape_all_exhibits(page)
        audio_data = await scrape_audio(page, n, save=save_audio_files)
        choices, choices_header = await get_choices(page)
        correct_label, correct_text = get_correct_info(choices)
        q_data     = await get_question(page)
        expl_data  = await get_explanation(page)
        meta       = await get_metadata(page)
        references = await get_references(page)

        print(f"    Choices: {len(choices)} | Correct: {correct_label or 'N/A'} — {(correct_text or '')[:40]}")
        print(f"    Exhibits: {len(exhibits)} | Audio: {len(audio_data)}")

        question_record = {
            "number":           n,
            "question_id":      qid,
            "question":         q_data["text"],
            "question_html":    q_data["html"],
            "choices_header":   choices_header,
            "choices":          choices,
            "correct_label":    correct_label,
            "correct_text":     correct_text,
            "explanation":      expl_data["text"],
            "explanation_html": expl_data["html"],
            "exhibits":         exhibits,
            "audio":            audio_data,
            "subject":          meta.get("subject"),
            "system":           meta.get("system"),
            "topic":            meta.get("topic"),
            "references":       references,
        }

        await save_question(question_record)
        await save_state(n)
        print(f"    [✓] Saved Q{n} to dataset")

        if n >= effective_max:
            print(f"\n[*] Reached limit ({effective_max}). Done!")
            break

        if not await click_next(page):
            print(f"\n[*] No Next button after Q{n} — end of test.")
            break

    print(f"\n[*] Scraping complete. {n - (start_from - 1)} questions saved.")
