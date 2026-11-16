"""Held-out retrieval probe (finding C-2): 40 paraphrased questions written by the reviewer, none in
the golden set, searched as the retrieval eval searches (kinds metric and glossary_term, viewer,
limit 10). Needs the settings of `python -m evals retrieval` (application database with the
index built with the real model). Run from the repository root:

    uv run python docs/reviews/core-complete-probes/heldout_retrieval.py
"""
from evals.runtime import open_runtime
from evals.retrieval import related_metrics, search_hits
from evals.scores import distinct_metrics
from makna.search.hybrid import HybridSearch

Q = [
 ("en","How much money did we lend out last month?",["disbursement_amount"]),
 ("en","How many loans were booked in Q1 2026?",["disbursed_contract_count"]),
 ("en","What is the typical loan amount per contract this year?",["average_ticket_size"]),
 ("en","What percentage of loan requests get accepted?",["approval_rate"]),
 ("en","How many loan requests came in during May 2026?",["application_count"]),
 ("en","How much do borrowers still owe us in total?",["outstanding_principal"]),
 ("en","What portion of our portfolio is in arrears over 90 days?",["npf_ratio"]),
 ("en","How much principal is stuck in loans overdue more than three months?",["npf_outstanding_principal"]),
 ("en","What fraction of the book is more than a month late?",["par30_ratio"]),
 ("en","How much cash came in from installment payments in June?",["collected_amount"]),
 ("en","How much were customers supposed to pay us in June 2026?",["due_amount"]),
 ("en","Are we collecting what is owed? Paid versus billed by month",["collection_rate"]),
 ("en","How much did we earn from fines for late payment?",["penalty_income"]),
 ("en","How many times did collectors contact overdue customers last quarter?",["collection_action_count"]),
 ("en","How often do debtors commit to paying after a collection call?",["promise_to_pay_rate","promise_to_pay_count"]),
 ("en","How many people signed up as customers in 2025?",["new_customer_count"]),
 ("en","How many accounts are still running at the end of June?",["active_contract_count"]),
 ("en","How much of our balance comes from rescheduled loans?",["restructured_outstanding_principal","restructured_ratio"]),
 ("en","How many credit decisions did we issue in April 2026?",["decided_application_count"]),
 ("en","How many applications got the green light last month?",["approved_application_count"]),
 ("id","Berapa dana yang kita salurkan bulan lalu?",["disbursement_amount"]),
 ("id","Berapa banyak kontrak baru yang cair kuartal ini?",["disbursed_contract_count"]),
 ("id","Berapa rata-rata nilai pinjaman per kontrak tahun ini?",["average_ticket_size"]),
 ("id","Berapa persen permohonan yang diterima?",["approval_rate"]),
 ("id","Ada berapa permohonan kredit yang masuk bulan Mei 2026?",["application_count"]),
 ("id","Berapa total utang pokok nasabah yang belum lunas?",["outstanding_principal"]),
 ("id","Berapa persen portofolio yang menunggak lebih dari 90 hari?",["npf_ratio"]),
 ("id","Berapa nilai pinjaman yang menunggak lebih dari tiga bulan?",["npf_outstanding_principal"]),
 ("id","Berapa bagian portofolio yang telat bayar lebih dari sebulan?",["par30_ratio"]),
 ("id","Berapa uang angsuran yang masuk bulan Juni?",["collected_amount"]),
 ("id","Berapa yang seharusnya dibayar nasabah pada Juni 2026?",["due_amount"]),
 ("id","Seberapa baik kita menagih? Perbandingan yang dibayar dengan yang ditagih",["collection_rate"]),
 ("id","Berapa pemasukan dari denda telat bayar?",["penalty_income"]),
 ("id","Berapa kali kolektor menghubungi nasabah menunggak kuartal lalu?",["collection_action_count"]),
 ("id","Seberapa sering nasabah berjanji akan membayar setelah ditagih?",["promise_to_pay_rate","promise_to_pay_count"]),
 ("id","Berapa orang yang mendaftar jadi nasabah di 2025?",["new_customer_count"]),
 ("id","Berapa rekening pembiayaan yang masih berjalan akhir Juni?",["active_contract_count"]),
 ("id","Berapa sisa pinjaman yang sudah dijadwal ulang?",["restructured_outstanding_principal","restructured_ratio"]),
 ("id","Berapa keputusan kredit yang dikeluarkan April 2026?",["decided_application_count"]),
 ("id","Berapa pengajuan yang lolos bulan lalu?",["approved_application_count"]),
]
rt = open_runtime()
systems = {
  "tuned 4.0/0.25": HybridSearch(rt.engine, rt.provider, rt.search),
  "keyword only": HybridSearch(rt.engine, rt.provider, rt.search.model_copy(update={"w_vector": 0.0})),
  "vector only": HybridSearch(rt.engine, rt.provider, rt.search.model_copy(update={"w_keyword": 0.0})),
  "start 1.0/1.0": HybridSearch(rt.engine, rt.provider, rt.search.model_copy(update={"w_keyword": 1.0, "w_vector": 1.0})),
  "1.0/0.5": HybridSearch(rt.engine, rt.provider, rt.search.model_copy(update={"w_keyword": 1.0, "w_vector": 0.5})),
}
out = {}
for name, s in systems.items():
    stats = {"en": [0,0,0], "id": [0,0,0]}  # r1, r3, n
    bands = {"high":0,"medium":0,"low":0}
    low_but_found = 0
    misses = []
    for lang, q, exp in Q:
        r = search_hits(s, q)
        rel = related_metrics(rt.engine, r.ids)
        ranked = [m.removeprefix("metric:") for m in distinct_metrics(r.ids, rel)]
        st = stats[lang]; st[2]+=1
        if ranked[:1] and ranked[0] in exp: st[0]+=1
        found = any(m in exp for m in ranked[:3])
        if found: st[1]+=1
        else: misses.append((q, ranked[:3]))
        bands[r.confidence.band]+=1
        if found and r.confidence.band == "low": low_but_found += 1
    out[name] = stats
    tot = [stats["en"][i]+stats["id"][i] for i in range(3)]
    print(f"{name:16s} all r@1 {tot[0]/tot[2]:.3f} r@3 {tot[1]/tot[2]:.3f} | en r@3 {stats['en'][1]}/{stats['en'][2]} | id r@3 {stats['id'][1]}/{stats['id'][2]} | bands {bands} | low although found in the first 3: {low_but_found}")
    if name.startswith("tuned"):
        for m in misses: print("    MISS", m)
