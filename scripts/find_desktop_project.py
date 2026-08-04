#!/usr/bin/env python3
"""Detect Flutter Windows, .NET Windows, or Flutter iOS projects."""
from __future__ import annotations
import argparse, json, re, sys
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORED={'.git','node_modules','build','.gradle','.dart_tool','.venv','venv','Pods','DerivedData','.idea','.vs'}

@dataclass(frozen=True)
class Candidate:
    path:str
    relative_path:str
    framework:str
    score:int
    depth:int
    reasons:tuple[str,...]
    entry_file:str|None=None

def read(path:Path)->str:
    try:return path.read_text(encoding='utf-8',errors='replace')
    except OSError:return ''

def allowed(root:Path,path:Path)->bool:
    try:rel=path.resolve().relative_to(root.resolve())
    except ValueError:return False
    return not any(part in IGNORED for part in rel.parts)

def add(out:list[Candidate],root:Path,path:Path,framework:str,score:int,reasons:list[str],entry:Path|None=None):
    if not allowed(root,path):return
    rel=path.resolve().relative_to(root.resolve()); depth=len(rel.parts); score-=min(depth,12)
    out.append(Candidate(str(path.resolve()),str(rel) if str(rel)!='.' else '.',framework,score,depth,tuple(reasons),str(entry.resolve()) if entry else None))

def flutter_candidates(root:Path,target:str,out:list[Candidate]):
    for pubspec in root.rglob('pubspec.yaml'):
        project=pubspec.parent
        if not allowed(root,project):continue
        body=read(pubspec);score=0;reasons=[]
        if re.search(r'(?ms)^\s*flutter\s*:\s*\n\s*sdk\s*:\s*["\']?flutter',body):score+=55;reasons.append('Flutter SDK dependency')
        if (project/'lib/main.dart').is_file():score+=40;reasons.append('lib/main.dart')
        elif (project/'lib').is_dir():score+=20;reasons.append('lib directory')
        if target=='windows':
            if (project/'windows').is_dir():score+=25;reasons.append('Windows platform')
            if score>=60:add(out,root,project,'flutter_windows',score,reasons,pubspec)
        elif target=='ios':
            if (project/'ios').is_dir():score+=25;reasons.append('iOS platform')
            if score>=60:add(out,root,project,'flutter_ios',score,reasons,pubspec)

def dotnet_candidates(root:Path,out:list[Candidate]):
    for csproj in root.rglob('*.csproj'):
        project=csproj.parent
        if not allowed(root,project):continue
        body=read(csproj)
        score=45;reasons=['C# project']
        if re.search(r'<UseWPF>\s*true\s*</UseWPF>',body,re.I):score+=40;reasons.append('WPF')
        if re.search(r'<UseWindowsForms>\s*true\s*</UseWindowsForms>',body,re.I):score+=40;reasons.append('Windows Forms')
        if re.search(r'<TargetFrameworks?>[^<]*-windows',body,re.I):score+=30;reasons.append('Windows TargetFramework')
        if re.search(r'<OutputType>\s*(WinExe|Exe)\s*</OutputType>',body,re.I):score+=20;reasons.append('Executable output')
        if re.search(r'<UseMaui>\s*true\s*</UseMaui>',body,re.I):
            continue  # .NET MAUI is reserved for the next compatibility stage.
        if score>=65:add(out,root,project,'dotnet_windows',score,reasons,csproj)

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('root',type=Path);ap.add_argument('--target',choices=('windows','ios'),required=True);ap.add_argument('--report',type=Path);args=ap.parse_args()
    root=args.root.resolve();out=[]
    flutter_candidates(root,args.target,out)
    if args.target=='windows':dotnet_candidates(root,out)
    out=sorted(out,key=lambda x:(-x.score,x.depth,x.relative_path.casefold()))
    report={'schema':1,'target':args.target,'selected':asdict(out[0]) if out else None,'candidate_count':len(out),'ambiguous':len(out)>1 and out[1].score>=out[0].score-5 if out else False,'candidates':[asdict(x) for x in out[:30]]}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not out:
        supported='Flutter Windows or .NET Windows' if args.target=='windows' else 'Flutter iOS'
        print(f'No supported {args.target} project was found. Supported: {supported}.',file=sys.stderr);return 5
    print(out[0].path);return 0
if __name__=='__main__':raise SystemExit(main())
