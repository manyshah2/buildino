#!/usr/bin/env python3
"""Detect the best Android-capable project in an extracted archive."""
from __future__ import annotations
import argparse, json, re
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORED={'.git','node_modules','build','.gradle','.dart_tool','.venv','venv','Pods','DerivedData'}

@dataclass(frozen=True)
class Candidate:
    path:str; relative_path:str; framework:str; score:int; depth:int; reasons:tuple[str,...]

def text(path:Path)->str:
    try:return path.read_text(encoding='utf-8',errors='replace')
    except OSError:return ''

def package_data(path:Path)->dict:
    try:
        value=json.loads(text(path)); return value if isinstance(value,dict) else {}
    except Exception:return {}

def add(candidates:list[Candidate], root:Path, project:Path, framework:str, score:int, reasons:list[str]):
    try: rel=project.resolve().relative_to(root.resolve())
    except ValueError:return
    if any(part in IGNORED for part in rel.parts):return
    depth=len(rel.parts); score-=min(depth,12)
    candidates.append(Candidate(str(project.resolve()),str(rel) if str(rel)!='.' else '.',framework,score,depth,tuple(reasons)))

def is_android_application(body:str)->bool:
    patterns=(
        r'\bid\s*\(?\s*["\']com\.android\.application["\']',
        r'\bapply\s+plugin\s*:\s*["\']com\.android\.application["\']',
        r'\balias\s*\(\s*libs\.plugins\.[A-Za-z0-9_.-]*(?:android\.application|application)[A-Za-z0-9_.-]*\s*\)',
    )
    return any(re.search(pattern,body) for pattern in patterns)

def nearest_gradle_root(root:Path, module:Path)->Path|None:
    current=module
    while True:
        if (current/'settings.gradle').is_file() or (current/'settings.gradle.kts').is_file():return current
        if current==root or current.parent==current:return None
        current=current.parent

def scan(root:Path)->list[Candidate]:
    root=root.resolve(); out=[]
    # Flutter
    for pubspec in root.rglob('pubspec.yaml'):
        project=pubspec.parent; body=text(pubspec); score=0; reasons=[]
        if re.search(r'(?ms)^\s*flutter\s*:\s*\n\s*sdk\s*:\s*["\']?flutter',body): score+=55; reasons.append('Flutter SDK dependency')
        if (project/'lib/main.dart').is_file(): score+=45; reasons.append('lib/main.dart')
        elif (project/'lib').is_dir(): score+=22; reasons.append('lib directory')
        if (project/'android').is_dir(): score+=20; reasons.append('Android platform')
        if score>=40:add(out,root,project,'flutter',score,reasons)
    # React Native / Expo
    for package in root.rglob('package.json'):
        project=package.parent; data=package_data(package)
        deps={}
        for key in ('dependencies','devDependencies','peerDependencies'):
            value=data.get(key,{})
            if isinstance(value,dict):deps.update(value)
        score=0; reasons=[]
        if 'react-native' in deps: score+=75; reasons.append('react-native dependency')
        if 'expo' in deps: score+=55; reasons.append('expo dependency')
        if (project/'android').is_dir(): score+=25; reasons.append('Android platform')
        if (project/'app.json').is_file() or (project/'app.config.js').is_file() or (project/'app.config.ts').is_file(): score+=8; reasons.append('app configuration')
        if any((project/name).is_file() for name in ('package-lock.json','yarn.lock','pnpm-lock.yaml')): score+=4; reasons.append('package manager lockfile')
        if score>=55:add(out,root,project,'react_native',score,reasons)
    # Buildozer/python-for-android
    for spec in root.rglob('buildozer.spec'):
        project=spec.parent; score=90; reasons=['buildozer.spec']
        if any((project/name).is_file() for name in ('main.py','src/main.py')):score+=15;reasons.append('Python entrypoint')
        add(out,root,project,'python_buildozer',score,reasons)
    # BeeWare Briefcase
    for marker in list(root.rglob('briefcase.toml'))+list(root.rglob('pyproject.toml')):
        project=marker.parent; body=text(marker)
        if marker.name=='briefcase.toml' or re.search(r'(?i)\bbriefcase\b|\[tool\.briefcase',body):
            score=85;reasons=[marker.name,'Briefcase configuration']
            add(out,root,project,'python_briefcase',score,reasons)
    # Chaquopy / Python embedded in Android Gradle
    seen=set()
    gradles=list(root.rglob('build.gradle'))+list(root.rglob('build.gradle.kts'))
    for gradle in gradles:
        body=text(gradle)
        if not re.search(r'(?i)chaquopy|com\.chaquo\.python',body):continue
        project=nearest_gradle_root(root,gradle.parent) or gradle.parent
        if project.resolve() in seen:continue
        seen.add(project.resolve()); add(out,root,project,'python_chaquopy',125,['Chaquopy Gradle plugin','Android Gradle project'])
    # Native Android Java/Kotlin. Candidate path is the Gradle root, not the module.
    native_roots:dict[Path,dict[str,object]]={}
    for gradle in gradles:
        rel=gradle.relative_to(root)
        if any(part in IGNORED for part in rel.parts):continue
        body=text(gradle)
        if not is_android_application(body):continue
        project=nearest_gradle_root(root,gradle.parent)
        if project is None:continue
        item=native_roots.setdefault(project.resolve(),{'score':80,'reasons':['Android Gradle settings','com.android.application plugin'],'modules':0,'kotlin':False,'java':False})
        item['modules']=int(item['modules'])+1
        module=gradle.parent
        if (module/'src/main/AndroidManifest.xml').is_file():item['score']=int(item['score'])+12;item['reasons'].append('AndroidManifest.xml')
        if any((module/'src/main').rglob('*.kt')):item['kotlin']=True
        if any((module/'src/main').rglob('*.java')):item['java']=True
        if module.name=='app':item['score']=int(item['score'])+8;item['reasons'].append('conventional app module')
    for project,item in native_roots.items():
        # Keep Flutter and React Native ahead when their generated Android folder is also a valid Gradle app.
        score=int(item['score'])
        ancestor=project.parent
        while ancestor != root.parent:
            pubspec=text(ancestor/'pubspec.yaml')
            package=package_data(ancestor/'package.json')
            deps={}
            for key in ('dependencies','devDependencies','peerDependencies'):
                value=package.get(key,{}) if isinstance(package,dict) else {}
                if isinstance(value,dict):deps.update(value)
            if re.search(r'(?ms)^\s*flutter\s*:\s*\n\s*sdk\s*:\s*["\']?flutter',pubspec) or 'react-native' in deps or 'expo' in deps:
                score-=45
                item['reasons'].append('nested platform of a higher-level framework')
                break
            if ancestor == root:break
            ancestor=ancestor.parent
        if bool(item['kotlin']):score+=8;item['reasons'].append('Kotlin sources')
        if bool(item['java']):score+=6;item['reasons'].append('Java sources')
        if (project/'gradlew').is_file():score+=8;item['reasons'].append('Gradle Wrapper')
        add(out,root,project,'native_android',score,list(dict.fromkeys(item['reasons'])))
    return sorted(out,key=lambda x:(-x.score,x.depth,x.relative_path.casefold(),x.framework))

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('root',type=Path);ap.add_argument('--report',type=Path);ap.add_argument('--framework');args=ap.parse_args()
    candidates=scan(args.root)
    if args.framework:candidates=[item for item in candidates if item.framework==args.framework]
    report={'schema':2,'selected':asdict(candidates[0]) if candidates else None,'candidate_count':len(candidates),'ambiguous':len(candidates)>1 and candidates[1].score>=candidates[0].score-5 if candidates else False,'candidates':[asdict(c) for c in candidates[:30]]}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not candidates:
        print('No supported Android project was found. Supported: Flutter, React Native, Native Android Java/Kotlin, Buildozer/python-for-android, BeeWare Briefcase, Chaquopy.',file=__import__('sys').stderr);return 5
    print(candidates[0].path);return 0
if __name__=='__main__':raise SystemExit(main())
