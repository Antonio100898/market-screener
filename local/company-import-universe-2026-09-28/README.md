# Retained-universe PostgreSQL import

Date: 2026-09-28. Branch: `codex/company-import`.

## Result

- SQLite dashboard-eligible `ok` companies: 6,925.
- Imported current PostgreSQL selections: 6,655.
- Canonical payload hash mismatches: 0.
- Withheld with exact reasons: 270.
- Verified evidence artifacts: 18,463.
- Current selections without artifact links: 0.
- One extra current PostgreSQL row, `PERSIST.NEW`, is retained development-test data and is not
  part of the SQLite universe.

Current imported security bases:

- `PRIMARY_COMMON_SHARE`: 4,542.
- `PRIMARY_DEPOSITARY_RECEIPT`: 228.
- `PRIMARY_ORDINARY_SHARE`: 2.
- `SEC_TICKER_MAPPING_ONLY`: 1,884, including the unrelated retained test row.

The first bulk pass imported 6,170 companies. Review found and fixed incomplete-cover routing and
receipt-provenance parity. Recovery added 485 companies. OrbStack froze during one full retry;
interrupting at the per-company boundary, restarting services, and resuming only missing rows
preserved every committed snapshot.

PostgreSQL and SeaweedFS were restarted after reconciliation. Real Uvicorn requests returned 200
for ABT, NTES, Panasonic `6752.T`, and mapping-only CATO with the same selected hashes.

## Withheld rows

These rows remain out of PostgreSQL current selections. No value or security identity was guessed.

### Missing depositary ratio — 11

ALTG, ASB, BAC, BNY, BRNS, CMS, FOCL, IEP, LOB, NNDM, QNRX

### Newer filing still pending structured facts — 112

AAPG, ADSE, ADXN, AGRO, AKAN, AMBR, APLM, APWC, ARBK, ATHE, BCH, BLIV, BMHL, BTDR,
BVN, BWAY, BWMX, CCU, CDRO, CEPU, CHAI, CMBT, CMCL, CNCK, CSAN, CUPR, CX, CYD, DEO,
DFSC, DGNX, DLXY, EC, ELLO, ELPC, ENIC, FMST, FNUC, FORTY, GFAI, GRO, GRRR, HAFN,
HDL, HEPS, HITI, HKPD, HTCO, HTOO, HUBC, HYFT, IFS, IGIC, IMOS, INCR, IONR, IVA,
JXG, KARO, KB, LANV, LOMA, LUXE, MAGH, MATH, MB, MESO, MRNO, MTLS, NAK, NGG, NTZ,
NVNI, OIO, OMSE, PAIYY, PAM, PAX, PBR, PHAR, PHI, PLRZ, RDHL, RYAAY, RYOJ, SBS,
SBSW, SGHC, SHMD, SID, SLSR, SQNS, SYNX, TGS, TII, TLK, TLSA, TLX, TM, TME, TNMG,
TRIB, TSM, TURB, UMC, VCIG, VIST, VLN, VRAX, WF, XRTX, XTLB

### Retained ticker not in current official SEC ticker map — 147

AACB, ACPS, ACQC, AFBI, AIDG, AIST, AIXN, ALOT, ALTB, AMPM, AMTU, APBD, APTOF,
ASRE, ATAI, AVAC, AVNI, BDDD, BEGI, BFLX, BLTG, BQST, BRBF, BSTR, BTECH, BTMCQ,
BYOC, BZRD, CCCP, CCLV, CDAQF, CLOW, CLSDQ, COOLU, CPRX, CRGT, CSC, CSGS, CUK,
CUX, DHIL, DMNIF, DSGT, DTII, DTLAP, DYNTQ, ECR, EEX, ELSE, EMPG, EXDW, FATAQ,
FBRX, FFGG, FGNV, FLYYQ, FNCHQ, FRZT, FTSP, FYNN, GKOR, GLCP, GLVT, GMTH, GOCOQ,
GROO, GTIJF, GZIC, IDIA, ILST, ILUS, IMTH, IOBTQ, ITRMF, IVPR, KAYS, KMCM, LAZR,
LNBY, MARH, MASN, MCLE, MCOM, MCW, MDAT, MFON, MSAV, MSH, MTMV, NFSN, NFTN,
NNAX, NORT, NOTVQ, NOWG, NTMJ, NXUR, PCAI, PHBI, PLMJF, PMDI, PNPL, PRIAF, PSPX,
RBCN, RDGA, RGPX, RKAM, RTEZ, RUIH, SALM, SBAQ, SCGY, SEAH, SENR, SFPT, SGTM,
SHMP, SNNC, SOCGM, SOHOB, SOHON, SSKN, TEAC, TESI, TFSA, TKOI, TLGYF, TLLTF,
TMRC, TPICQ, TWNPQ, UCLE, UNTC, UPX, VFL, VIASP, WELNF, WHEN, WHLM, WLSS, WNLV,
XITO, XXAAU, ZHJD, ZRCN, ZYXIQ

## Verification

- Exhaustive canonical SHA-256 comparison: 6,655 equal, 0 different.
- Full runtime payload regression: no field moved across 6,925 companies.
- Audit: 279 sourced and 3,411 arithmetic figures passed; zero wrong.
- Filing audit: 378 published-statement figures passed; zero wrong.
- Storage-enabled Python suite after the final contract: 736 passed.
- Web suite: 113 passed.
- Alembic head: `20260928_0004`; no pending operation.
