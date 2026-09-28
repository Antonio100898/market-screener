# Incomplete Company Facts inventory

All 18 engine-184 `incomplete_company_facts` rows were checked against the retained SEC Company
Facts response for their latest annual accession.

| Ticker | CIK | Annual accession | Bytes | SHA-256 | Facts from accession |
|---|---|---|---:|---|---:|
| AERO | 0001561861 | 0001193125-26-197494 | 802 | `004f852aefe86e99890486a5760c7485a6a5b2c3ad7ca0288607f2be0784766d` | 1 |
| AGMB | 0002020932 | 0001104659-26-047872 | 790 | `ba80f53afbc7b657c6696bc171c4a1715a04ad63338c6d1d8e07b4c5d3b95562` | 1 |
| AHNRF | 0001304409 | 0001683168-26-002801 | 1,374,683 | `33adfd92b748722009a7d4269d9ba1f41297231043b92396424dd39954942223` | 1 |
| ALPS | 0002025774 | 0001493152-26-036364 | 780 | `0046003aba5faf359ed72653f27278af9eb1d40245bb4e734bf37be4f31bac08` | 1 |
| AUGO | 0001468642 | 0001171843-26-002783 | 788 | `1a799d7bfa91c327c9c584d2fa9d7e281559f9a4ef5a36828daa51f40fd5baf7` | 1 |
| BRBI | 0002058601 | 0001213900-26-050426 | 790 | `3d787658e570fd3f54de3e73af1b3c02fd93062918cdfeb178a9dabd3be26411` | 1 |
| CIB | 0002058897 | 0002058897-26-000125 | 786 | `c359efed1291e6e9791b295144e5a779daad0e5f627bcc0ad47c8b361a223a3f` | 1 |
| CNI | 0000016868 | 0001104659-26-010352 | 1,181,146 | `6ccf0b631dedd75ff5bad530ce5b0e22ed5e864515e2447709366fcb24b8ec40` | 1 |
| DAVI | 0002039072 | 0001683168-26-003530 | 3,127 | `ef1512d647daaf49b12166edc3781227cbe733885af526fbf0b30724f534ff75` | 4 |
| GCDT | 0001926293 | 0001493152-26-038530 | 36,845 | `3dd448edeb63d6314eb697c33c6a456cf0603f0974d5b6aca22301b3b7efcb8a` | 1 |
| GMTL | 0002039972 | 0001104659-26-108373 | 1,809 | `28830b901290692f4ccac74b8fed8c5b66982e3e8a1053ad8993eabb18040d77` | 1 |
| HBNB | 0002054507 | 0001213900-26-049771 | 799 | `26c5a3fbc2fb01fb60d9b08bfc5cd47b716fcbf1dd3f0e098e3626ce663ba6f6` | 1 |
| NXAT | 0002000756 | 0001829126-26-005357 | 8,484 | `30b5e4e98ee79a61b96cb745f9bd75357370b328fb8428d312181a0bcc783706` | 1 |
| PAYP | 0002080845 | 0001193125-26-289382 | 784 | `5ae6fde87fdf84aa848be025ef999dfc9c5a840401149ff6b73800ec7bcc3ab4` | 1 |
| PICS | 0001841644 | 0001213900-26-049950 | 309 | `e1407a9346ead9f47164cec3214f14d8231f570e45335780737eef1304670372` | 1 |
| TMCR | 0002087398 | 0001104659-26-049527 | 2,355 | `14f68a7468ca88d87a2c267333d031c494481d6c26ce382e5da9e1a81b32443b` | 1 |
| VMET | 0002080073 | 0001104659-26-052847 | 798 | `f037a73468842ea23cdf7ec78c1d818105b263c38bd7ed4bbe9a0040ceb2b23b` | 1 |
| YMAT | 0001875016 | 0001493152-26-019802 | 3,220 | `1486703fcc6f9adfc55f154cf4a0001d0b0a99fc9e77bb33a48dfd4808230223` | 4 |

Fifteen responses expose one current-accession fact. It is normally only the DEI share count.
DAVI exposes employee, share-count, and segment-count facts. YMAT exposes the share count, par
value, and two bank-loan facts. None exposes a standard annual balance anchor in the current 20-F
or 40-F accession.

Retained DERA sidecars exist for AHNRF, ALPS, and CNI. The current admission gate reads the annual
standard balance from Company Facts before dimensioned facts are considered, so these sidecars do
not establish the missing statement basis.
