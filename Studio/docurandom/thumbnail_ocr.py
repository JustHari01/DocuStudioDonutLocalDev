"""Independent local Japanese OCR and measured headline colours."""
import io,re,threading,unicodedata
import numpy as np
from PIL import Image
from .storage import app_data_dir
from .thumbnail_design import extract_headlines, normal

_ENGINE=None
_FALLBACK_ENGINE=None
_LATIN_ENGINE=None
_LOCK=threading.Lock()


def lexical_normal(text):
    # OCR can split a three-dot ellipsis into unlabelled image components.
    # Preserve every letter, digit and other punctuation. The independent
    # exact-image semantic review still must confirm the complete headline.
    return re.sub(r'\.{3,}', '', normal(text))


def line_groups(rows):
    """Keep small machine labels apart from large headlines at the same Y."""
    lines=[]
    for row in sorted(rows,key=lambda r:sum(p[1]for p in r['box'])/4):
        y=sum(p[1]for p in row['box'])/4
        height=max(p[1]for p in row['box'])-min(p[1]for p in row['box'])
        line=next((g for g in lines if abs(g['y']-y)<25 and .55<height/max(g['height'],1)<1.8),None)
        if line is None:line={'y':y,'height':height,'items':[]};lines.append(line)
        line['items'].append(row)
    groups=[]
    for line in lines:
        group=None;right=None
        for row in sorted(line['items'],key=lambda r:min(p[0]for p in r['box'])):
            left=min(p[0]for p in row['box'])
            if group is None or left-right>max(40,line['height']*.85):
                group={'y':line['y'],'items':[]};groups.append(group)
            group['items'].append(row);right=max(p[0]for p in row['box'])
    return groups


def verify_leading_latin(group, expected, rgb):
    """Read an ambiguous leading Latin glyph independently, never alias 乙 to Z."""
    prefix=re.match(r'[A-Za-z0-9]{1,3}(?=[\u3040-\u30ff\u3400-\u9fff])',expected)
    if not prefix:return None
    prefix=prefix[0];text=''.join(r['txt']for r in group['items'])
    suffix=lexical_normal(expected[len(prefix):]);observed=lexical_normal(text)
    if not observed.endswith(suffix) or not 1<=len(observed)-len(suffix)<=len(prefix):return None
    points=[p for r in group['items']for p in r['box']]
    x0,y0=min(p[0]for p in points),min(p[1]for p in points)
    y1=max(p[1]for p in points);height=y1-y0
    if height<40:return None
    left=max(0,int(x0)-8);top=max(0,int(y0)-12)
    right=min(rgb.shape[1],int(x0+height*(len(prefix)+.03)))
    bottom=min(rgb.shape[0],int(y1)+24)
    global _LATIN_ENGINE
    from rapidocr import RapidOCR,LangRec,OCRVersion,ModelType
    with _LOCK:
        if _LATIN_ENGINE is None:
            _LATIN_ENGINE=RapidOCR(params={'Global.model_root_dir':str(app_data_dir()/'OCR models'),
                'Global.log_level':'warning','Det.ocr_version':OCRVersion.PPOCRV4,'Det.model_type':ModelType.MOBILE,
                'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.EN,
                'EngineConfig.onnxruntime.intra_op_num_threads':4,'EngineConfig.onnxruntime.inter_op_num_threads':1})
        rows=_LATIN_ENGINE(rgb[top:bottom,left:right]).to_json()or[]
    if isinstance(rows,str):
        import json
        rows=json.loads(rows)
    verified=next((r for r in rows if normal(r['txt'])==normal(prefix) and r['score']>=.90
        and min(p[0]for p in r['box'])<height*.35),None)
    if verified is None:return None
    return {'verified_text':prefix+observed[-len(suffix):], 'original_text':text,
            'leading_latin_ocr':verified,'crop':[left,top,right,bottom]}


def inspect(payload,prompt):
    global _ENGINE, _FALLBACK_ENGINE
    from rapidocr import RapidOCR,LangRec,OCRVersion,ModelType
    with _LOCK:
        if _ENGINE is None:
            _ENGINE=RapidOCR(params={'Global.model_root_dir':str(app_data_dir()/'OCR models'),'Global.log_level':'warning',
                'Det.ocr_version':OCRVersion.PPOCRV4,'Det.model_type':ModelType.MOBILE,
                'Rec.ocr_version':OCRVersion.PPOCRV4,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.JAPAN,
                'EngineConfig.onnxruntime.intra_op_num_threads':4,'EngineConfig.onnxruntime.inter_op_num_threads':1})
        # RGB arrays avoid version-specific byte transport interpretations.
        rgb = np.asarray(Image.open(io.BytesIO(payload)).convert('RGB'))
        result=_ENGINE(rgb).to_json()or[]
        if isinstance(result, str):
            import json
            result=json.loads(result)
        expected=extract_headlines(prompt)
        result_sets=[result]
        def exact_rows(rows):
            strings=[lexical_normal(''.join(r['txt']for r in g['items']))for g in line_groups(rows)]
            return sum(lexical_normal(text)in strings for text in expected)
        # Some display fonts lose Latin suffixes in the Japanese recognizer.
        # A second independent multilingual model can verify the same bytes;
        # neither model is allowed to guess or rewrite a missing character.
        if result and exact_rows(result) < 3:
            if _FALLBACK_ENGINE is None:
                _FALLBACK_ENGINE=RapidOCR(params={'Global.model_root_dir':str(app_data_dir()/'OCR models'),
                    'Global.log_level':'warning','Det.ocr_version':OCRVersion.PPOCRV4,
                    'Det.model_type':ModelType.MOBILE,'Rec.ocr_version':OCRVersion.PPOCRV5,
                    'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.CH,
                    'EngineConfig.onnxruntime.intra_op_num_threads':4,'EngineConfig.onnxruntime.inter_op_num_threads':1})
            alternate=_FALLBACK_ENGINE(rgb).to_json()or[]
            if isinstance(alternate,str):
                import json
                alternate=json.loads(alternate)
            result_sets.append(alternate)
    if not result:
        return {'passed':False,'status':'unavailable','headline_checks':[], 'all_ocr':[],
                'method':'Japanese OCR','limits':'No text recognized; this is not proof of absent or incorrect text.'}
    im=Image.open(io.BytesIO(payload)).convert('RGB');a=np.asarray(im)
    groups=[g for rows in result_sets for g in line_groups(rows)]
    expected=extract_headlines(prompt)
    normalize=lexical_normal
    # Micro labels and machine print can precede a headline in OCR order.
    # Match complete Japanese strings instead of accepting the first three rows.
    selected=[]
    used=set()
    for text in expected:
        candidates=[(i,g) for i,g in enumerate(groups) if i not in used and
                    normalize(''.join(r['txt'] for r in sorted(g['items'],key=lambda r:min(p[0]for p in r['box']))))==normalize(text)]
        if not candidates:
            for i,g in enumerate(groups):
                if i not in used and (proof:=verify_leading_latin(g,text,a)):
                    candidates=[(i,{**g,**proof})];break
        if not candidates:
            return {'passed':False,'status':'mismatch','headline_checks':selected,'all_ocr':result,
                    'method':'Japanese OCR','limits':'An exact headline was not verified.'}
        index,group=candidates[0];used.add(index);selected.append(group)
    evidence=[];passed=len(expected)==3
    for index,(group,color)in enumerate(zip(selected,('yellow','cyan','red'))):
        items=sorted(group['items'],key=lambda r:min(p[0]for p in r['box']))
        text=group.get('verified_text')or''.join(r['txt']for r in items)
        coords=[p for r in items for p in r['box']]
        x0,x1=max(0,int(min(p[0]for p in coords))),min(im.width,int(max(p[0]for p in coords))+1)
        y0,y1=max(0,int(min(p[1]for p in coords))),min(im.height,int(max(p[1]for p in coords))+1)
        roi=a[y0:y1,x0:x1].astype(np.int16);r,g,b=roi[:,:,0],roi[:,:,1],roi[:,:,2]
        mask={'yellow':(r>170)&(g>150)&(b<120),'cyan':(r<140)&(g>150)&(b>150),'red':(r>150)&(g<140)&(b<140)}[color]
        ratio=float(mask.mean());word_ok=index<len(expected)and normalize(text)==normalize(expected[index])
        uncropped=x0>1 and y0>1 and x1<im.width-1 and y1<im.height-1
        ok=word_ok and ratio>.16 and uncropped and min(z['score']for z in items)>.85
        passed=passed and ok
        evidence.append({'text':text,'expected':expected[index] if index<len(expected) else None,'colour':color,
                         'leading_latin_verification':group.get('leading_latin_ocr'),
                         'original_text':group.get('original_text'), 'verification_crop':group.get('crop'),
                         'colour_pixel_ratio':ratio,'uncropped':uncropped,'passed':ok,'bounds':[x0,y0,x1,y1],
                         'exact_punctuation_read':normal(text)==normal(expected[index]),
                         'ellipsis_requires_semantic_review':normal(text)!=normal(expected[index])})
    return {'passed':bool(passed and len(evidence)==3),'method':'Japanese/multilingual PP-OCR + isolated Latin glyph verification + RGB foreground measurements',
            'headline_checks':evidence,'all_ocr':result,'limits':'Latin case and ellipses are normalized only for OCR; the separate semantic review must verify exact full headlines, subject and flags.'}
