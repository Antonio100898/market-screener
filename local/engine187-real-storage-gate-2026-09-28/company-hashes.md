# Company hashes and artifact roles

## Canonical and snapshot hashes

| Ticker | Snapshot ID | Payload SHA-256 | Snapshot SHA-256 |
|---|---:|---|---|
| ABT | 1 | `3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f` | `8e7d61bd1441d916f1d2d11e5b1242955195b56014ace29ec504d07f73fd440e` |
| NTES | 3 | `5d6501bf32b5a30a6c08b16f879425351f1a7c4e8cade72650ef0cd9d7b63496` | `9dfbda5041ce740c5a824e625a4e7fdb1ffc73fa1453e612c567b3bf1b64d953` |
| AERO | 4 | `db7c02a7c1664ae47f39d4d15368a54eb25df76544486c862aa28a0dbcf0abad` | `d9997d75d0cfdcedbc74536d118405ca20c2a84a180419111bad3de9f6a174d9` |
| CNI | 5 | `f2ef3efb6f7622df3a67ca1b349d9179174d8ab277af04c2d16c7cd80fd1ded6` | `6de480b8c1e02bbe705907644b441aff8ee24fa935ffae965154e76f775bc1fa` |
| 6752.T | 6 | `5593ade3e5bad5f63ee630a5dc02c80c81ee09b1534fef269e56ff75cfb4330d` | `c0200925aecc5f50f49801535f4b985941b74d01675c792d505f3b613e7c5f86` |
| 7974.T | 7 | `616dc4fd05158c59570bcca7273390c0197c7e55a123041a8339b8843ec29a04` | `032408cdb7db6dc6ebd236bea5aa6c3ffb493672ce2ffdd6691315526230de0e` |
| CATO | 8 | `4d757a5939529317b050aa734c31e98330e101afe7a3596bf34d189e0363dd35` | `1091a2374e0fa6ac9e993e12bbda32cf3a50c823d22620bd41f85b9beee73646` |

## New SEC direct path: AERO

| Role | SHA-256 |
|---|---|
| `official_api_facts` | `004f852aefe86e99890486a5760c7485a6a5b2c3ad7ca0288607f2be0784766d` |
| `sec_inline_current_manifest` | `b096381fcc30d00e92b0dbe1e1df29d433f853055903ea9935f4dde24f8c2f39` |
| `sec_inline_direct_annual_filing_summary` | `3ff95b8a90d77891c06f3afef1e8a21fbb95a3cf9cf2df79f8564ab253052764` |
| `sec_inline_direct_annual_index` | `eedf4cfda3a6a7f923464b3321abd4eb3be4e4154cd1bcd53da113f439449b4c` |
| `sec_inline_direct_annual_instance` | `3e6614e5acfdb677750d9189e9aac2cdd1417c212aa751681b22e84caee43b19` |
| `sec_inline_direct_annual_presentation` | `30f70090d1860858457af1d381235756fc3109bdb8a87f1c50274e2810217479` |
| `sec_inline_direct_annual_primary_document` | `b842756f7ce62b0ae1bd2cf14d6fc3373c61297c0a74934f020493332415326e` |
| `sec_inline_direct_annual_schema` | `30f70090d1860858457af1d381235756fc3109bdb8a87f1c50274e2810217479` |
| `structured_cover_identity` | `a86de3bc647c451aae6590c98ee2f28f78a1591e51f7aacadd220072f7032745` |

## New SEC incorporated path: CNI

| Role | SHA-256 |
|---|---|
| `official_api_facts` | `6ccf0b631dedd75ff5bad530ce5b0e22ed5e864515e2447709366fcb24b8ec40` |
| `dimensioned_facts` | `bb5baa2b491aa1762167299c567a8022c4b13b0d9f20efff09b8750ac000d130` |
| `sec_inline_current_manifest` | `c4a1df2bb6fc9e253ecde8aecebfe4fc6cd5f85f6c9b3930245ee28ad8a3db10` |
| `sec_inline_annual_wrapper_index` | `50e83ef941db63b3d5020c0d851837cd873dcaedebe220cb4fb22db8d4624c89` |
| `sec_inline_annual_wrapper_primary_document` | `7490bb0e34394e55e1e349e7f3c44581fe3bfe50ee02b368a1c4b938af13ab2f` |
| `sec_inline_incorporated_source_filing_summary` | `6bbe88ca9cbbff7468ade8ed263ec128bd84841a6feba282bfd89a25d3ca6cca` |
| `sec_inline_incorporated_source_index` | `78fc8952f13a716fd9606b5ac2812020fcb6d84da3f58605b518a268868f2778` |
| `sec_inline_incorporated_source_instance` | `83d63da8c1367aa525db38b13d436536746cf615e6ce0a9c22ef1dc8b322e9d2` |
| `sec_inline_incorporated_source_presentation` | `26a61742ea6898eaec749380102749f28cf092ba14e87ff28c89d60c5e3566db` |
| `sec_inline_incorporated_source_primary_document` | `a3789c3b3a1a2a1b2ee15a6985c6e757c699c8c2992436ec6036b4e985fbb79d` |
| `sec_inline_incorporated_source_schema` | `6b5abc09e7f60586c4b176b326f40b198e81e2bdd70695656fb527dd73a6ff5c` |
| `structured_cover_identity` | `4d21ad790e6edcf50f27f13e0c700cfb4aeae7f265b82b545532854d93186721` |

## New EDINET path: Nintendo

| Role | SHA-256 |
|---|---|
| `canonical_edinet_facts` | `536edbf2940db01a3d98e1fbaaba9bcc74e2f5f4656675e6d618de3031540c12` |
| `raw_filing` | `a9eb08b6687ec604fddcf997ffb8c97867084e4353d372afeb7637456981e73d` |

Panasonic retained its existing two roles:

- `canonical_edinet_facts` — `c3aa914176544f50d19f3c0ffb16fab67f6e6170d9735939ddd9af3622e11ac4`
- `raw_filing` — `95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f`

ABT, NTES, and CATO retained their previously pinned artifact roles and hashes. The integration
test checks those boundaries directly.
