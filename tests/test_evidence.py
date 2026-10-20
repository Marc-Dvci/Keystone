from keystone.evidence import run_all


async def test_every_evidence_case_passes():
    rows = await run_all()
    assert [r["case"] for r in rows if not r["pass"]] == []
