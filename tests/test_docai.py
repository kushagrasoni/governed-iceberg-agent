import json

from governed_agent import docai
from governed_agent.lakehouse import read


def test_extraction_confidence_and_routing(tmp_path):
    docs = tmp_path / "docs"
    docai.generate_documents(docs, n=8, seed=7)
    exs = [docai.extract(p) for p in sorted(docs.glob("*.txt"))]
    clean = exs[0]
    assert clean.min_confidence >= 0.9
    assert exs[1].fields["annual_income"].confidence < 0.9          # OCR 0 -> O
    assert exs[2].fields["loan_amount"].value is None               # smudged digit
    assert exs[3].fields["application_date"].confidence < 0.9       # ambiguous MM/DD/YY
    q = docai.ReviewQueue(tmp_path / "q.jsonl")
    routed = docai.route(exs, q, threshold=0.9)
    assert len(routed["ready"]) == 2 and len(routed["held"]) == 6 and len(q.open_items()) == 6
    assert all(r["source_sha256"] and r["extractor"] for r in routed["ready"])


def test_low_confidence_fields_wait_for_reviewer_then_ingest_through_gate(gov, tmp_path):
    docs = tmp_path / "docs"
    docai.generate_documents(docs, n=8, seed=7)
    routed, pairs = gov.docai_ingest(docs)
    assert len(pairs) == 1 and len(routed["held"]) == 6
    plan, prov = pairs[0]
    assert plan.operation == "APPEND_RECORDS"
    assert {d["sha256"] for d in prov["inputs"] if d["kind"] == "document"} == {r["source_sha256"] for r in routed["ready"]}
    # regulated table: auto-approve is not enough
    out = gov.run_plan(plan, prov, approver=None, auto_approve_low_risk=True)
    assert out.status == "REJECTED"
    loans = gov.catalog.load_table("bank.loan_applications")
    assert read(loans).num_rows == 0
    routed, pairs = gov.docai_ingest(docs)
    out = gov.run_plan(*pairs[0], approver="reviewer.alex", simulated_decision="yes")
    assert out.status == "EXECUTED" and read(gov.catalog.load_table("bank.loan_applications")).num_rows == 2

    key = json.loads((docs / "_answer_key.json").read_text())
    for item in gov.queue.open_items():
        gov.queue.resolve(item["item_id"], str(key[item["doc"]][item["field"]]), "reviewer.alex")
    routed, pairs = gov.docai_ingest(docs)
    out = gov.run_plan(*pairs[0], approver="reviewer.alex", simulated_decision="yes")
    assert out.status == "EXECUTED"
    rows = read(gov.catalog.load_table("bank.loan_applications")).to_pylist()
    assert len(rows) == 8
    reviewed = [r for r in rows if r["reviewed_by"]]
    assert len(reviewed) == 6 and all(r["min_confidence"] < 0.9 for r in reviewed)
    by_doc = {r["source_doc"]: r for r in rows}
    for doc, truth in key.items():
        assert by_doc[doc]["loan_amount"] == truth["loan_amount"]
    # idempotent: nothing left to ingest
    _, pairs = gov.docai_ingest(docs)
    assert pairs == []
