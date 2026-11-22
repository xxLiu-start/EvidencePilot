"""Deterministic provisional grading against independently authored quote qrels.

No judge-model call. All numerators/denominators are persisted for audit.
"""
import math
import re


def compact(text):
    return " ".join(text.casefold().split())


def grade_answer(task, answer, sources, retrieved_chunks):
    pages = {page["source_id"]: page for page in sources}
    chunks = {chunk["id"]: chunk for chunk in retrieved_chunks}
    answers = {item["claim_id"]: item for item in answer.get("claims", [])}
    required = {claim["id"]: claim for claim in task["required_claims"]}
    counts = {"correct_claims": 0, "required_claims": len(required), "covered_claims": 0,
              "valid_citations": 0, "citations": 0, "unsupported_claims": 0, "asserted_claims": 0}
    details = []
    for claim_id, gold in required.items():
        item = answers.get(claim_id, {"status": "MISSING", "answer": "", "citations": []})
        valid_sources = set()
        bad_citations = []
        for citation in item.get("citations", []):
            counts["citations"] += 1
            chunk = chunks.get(citation.get("evidence_id"))
            quote = citation.get("supporting_text", "")
            valid = False
            if chunk and len(quote.strip()) >= 12 and quote in chunk["text"]:
                page = pages[chunk["source_id"]]
                refs = [ref for ref in gold["reference_evidence"] if ref["source_id"] == chunk["source_id"]]
                # A real contiguous quote must overlap a specific independently
                # annotated passage, not merely come from a relevant webpage.
                valid = any((compact(quote) in compact(ref["supporting_text"]))
                            or compact(ref["supporting_text"]) in compact(quote) for ref in refs)
                valid = valid and page["source_id"] in task["allowed_source_ids"]
                if gold["expected_status"] == "OUTDATED":
                    valid = valid and item["status"] == "OUTDATED"
                elif gold["expected_status"] != "SUPPORTED":
                    valid = False
                if valid:
                    valid_sources.add(page["source_id"])
            counts["valid_citations"] += int(valid)
            if not valid:
                bad_citations.append(citation.get("evidence_id"))
        cited = bool(gold["required_source_groups"]) and all(set(group) & valid_sources for group in gold["required_source_groups"])
        text_ok = all(re.search(pattern, item.get("answer", ""), re.I | re.S) for pattern in gold["answer_patterns"])
        correct = item["status"] == gold["expected_status"] and text_ok and (cited if gold["expected_status"] == "SUPPORTED" else True)
        covered = gold["expected_status"] == "SUPPORTED" and cited
        asserted = item["status"] in {"SUPPORTED", "CONTRADICTED"}
        unsupported = asserted and not (cited and text_ok and item["status"] == gold["expected_status"])
        counts["correct_claims"] += int(correct)
        counts["covered_claims"] += int(covered)
        counts["asserted_claims"] += int(asserted)
        counts["unsupported_claims"] += int(unsupported)
        details.append({"claim_id": claim_id, "correct": bool(correct), "covered": bool(covered),
                        "unsupported": bool(unsupported), "answer_patterns_passed": bool(text_ok),
                        "expected_status": gold["expected_status"], "actual_status": item["status"],
                        "valid_source_ids": sorted(valid_sources), "invalid_citation_ids": bad_citations})
    # Extra claims are not silently ignored: unsupported assertions penalize the method.
    for key, item in answers.items():
        if key not in required:
            counts["citations"] += len(item.get("citations", []))
            if item.get("status") in {"SUPPORTED", "CONTRADICTED"}:
                counts["asserted_claims"] += 1
                counts["unsupported_claims"] += 1
    return {"counts": counts, "details": details,
            "task_correct": counts["correct_claims"] == counts["required_claims"] and set(answers) == set(required),
            "claim_accuracy": counts["correct_claims"] / counts["required_claims"]}


def calculate_metrics(results):
    if not results:
        raise ValueError("Cannot score an empty experiment")
    def total(key):
        return sum(row["grading"]["counts"][key] for row in results)
    def ratio(a, b):
        return total(a) / total(b) if total(b) else None
    token_rows = [row for row in results if row.get("token_cost") is not None]
    return {"answer_accuracy": sum(row["grading"]["task_correct"] for row in results)/len(results),
            "claim_accuracy": ratio("correct_claims", "required_claims"),
            "evidence_coverage": ratio("covered_claims", "required_claims"),
            "citation_precision": ratio("valid_citations", "citations"),
            "unsupported_claim_rate": ratio("unsupported_claims", "asserted_claims"),
            "avg_retrieval_calls": sum(row["retrieval_calls"] for row in results)/len(results),
            "avg_latency": sum(row["latency_seconds"] for row in results)/len(results),
            "avg_token_cost": sum(row["token_cost"] for row in token_rows)/len(token_rows) if len(token_rows) == len(results) else None,
            "token_usage_available_tasks": len(token_rows), "task_count": len(results),
            "counts": {key: total(key) for key in results[0]["grading"]["counts"]}}
