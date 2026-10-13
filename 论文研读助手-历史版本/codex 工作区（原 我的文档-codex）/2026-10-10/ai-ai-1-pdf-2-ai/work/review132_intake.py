from pathlib import Path, PurePosixPath
import ast, hashlib, json, zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "review-1.3.2"
ZIP = Path(r"D:\harness home\releases\1.3.2\论文研读助手-本地版-1.3.2-全新安装.zip")
MAN = Path(r"D:\harness home\releases\1.3.2-待审清单.json")
manifest = json.loads(MAN.read_text(encoding="utf-8-sig"))
results = []
with zipfile.ZipFile(ZIP) as z:
    seen = set()
    entries = []
    for i in z.infolist():
        p = PurePosixPath(i.filename)
        assert not p.is_absolute() and ".." not in p.parts and "\\" not in i.filename
        assert not (i.external_attr >> 16 & 0o170000 == 0o120000)
        folded = i.filename.casefold()
        assert folded not in seen
        seen.add(folded)
        assert i.file_size < 100_000_000
        entries.append({"path": "/".join(p.parts[1:]), "size": i.file_size})
        rel = PurePosixPath(*p.parts[1:])
        # Extract code/assets only. Never read/copy the user's library or PDFs.
        if rel.parts and rel.parts[0] != "data" and rel.suffix in {".py", ".js", ".html", ".css", ".txt", ".md"}:
            dest = OUT.joinpath(*rel.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            b = z.read(i)
            dest.write_bytes(b)
    archive = {x["path"]: x for x in entries}
for f in manifest["files"]:
    path = Path(f["path"])
    # Windows path parsing is native here.
    rel = str(path.relative_to(Path(manifest["project_root"]))).replace("\\", "/")
    local = OUT / rel
    row = {"path": rel, "in_archive": rel in archive}
    if local.is_file():
        b = local.read_bytes()
        row.update(hash_match=hashlib.sha256(b).hexdigest()[:16].upper() == f["sha256_16"],
                   size_match=len(b) == f["bytes"])
    if path.is_file():
        b = path.read_bytes()
        row["working_copy_hash_match"] = hashlib.sha256(b).hexdigest()[:16].upper() == f["sha256_16"]
    results.append(row)
syntax = []
for p in OUT.glob("*.py"):
    ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
    syntax.append(p.name)
report = {
    "zip_sha256": hashlib.sha256(ZIP.read_bytes()).hexdigest(),
    "manifest_sha256": hashlib.sha256(MAN.read_bytes()).hexdigest(),
    "zip_hash_matches_manifest": hashlib.sha256(ZIP.read_bytes()).hexdigest().upper() == manifest["artifacts"]["fresh_install_zip_sha256"],
    "files": results, "archive_entries": entries, "syntax_ok": syntax,
    "user_data_not_read_or_extracted": True,
    "sensitive_test_file_exists_not_read": Path(manifest["project_root"], "tests", "secret.json").is_file(),
}
(ROOT / "review132_intake.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k:v for k,v in report.items() if k not in {"archive_entries"}}, ensure_ascii=True, indent=2))
