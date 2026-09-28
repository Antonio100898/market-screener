# Per-run results

| Provider | Case | Arm | State | Accepted | Failed | Unscored | Input tokens | Output tokens | Cost USD |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| openai | cni-wrapper-neighbour | baseline | completed | 7 | 0 | 0 | 31905 | 1045 | 0.003713 |
| openai | cni-wrapper-neighbour | candidate | completed | 7 | 0 | 0 | 32208 | 1984 | 0.0042128 |
| fireworks | cni-wrapper-neighbour | baseline | completed | 7 | 0 | 0 | 32021 | 3902 | 0.00675415 |
| fireworks | cni-wrapper-neighbour | candidate | completed | 7 | 0 | 0 | 32324 | 7937 | 0.0088171 |
| google | cni-wrapper-neighbour | baseline | http_error | 0 | 0 | 7 |  |  | 0 |
| anthropic | cni-wrapper-neighbour | baseline | completed | 7 | 0 | 0 | 47308 | 1648 | 0.111096 |
| anthropic | cni-wrapper-neighbour | candidate | completed | 0 | 7 | 0 | 47763 | 96 | 0.096486 |
| openai | cni-exhibit | baseline | http_error | 0 | 0 | 7 |  |  | 0 |
| fireworks | cni-exhibit | baseline | completed | 0 | 7 | 0 | 228012 | 5201 | 0.0368023 |
| fireworks | cni-exhibit | candidate | completed | 0 | 7 | 0 | 228315 | 6975 | 0.03773475 |
| anthropic | cni-exhibit | baseline | completed | 0 | 7 | 0 | 342625 | 2083 | 0.70608 |
| anthropic | cni-exhibit | candidate | completed | 0 | 7 | 0 | 343080 | 2101 | 0.70717 |
| fireworks | aero-summary-neighbour | baseline | completed | 0 | 1 | 0 | 505863 | 2834 | 0.07729645 |
| fireworks | aero-summary-neighbour | candidate | completed | 0 | 1 | 0 | 506166 | 3518 | 0.0776839 |
| anthropic | aero-summary-neighbour | baseline | completed | 0 | 1 | 0 | 761194 | 360 | 1.525988 |
| anthropic | aero-summary-neighbour | candidate | completed | 1 | 0 | 0 | 761649 | 318 | 1.526478 |
| fireworks | aero-primary | baseline | completed | 0 | 7 | 0 | 506224 | 7613 | 0.0797401 |
| fireworks | aero-primary | candidate | provider_shape_error | 0 | 0 | 7 | 506527 | 8192 | 0.08007505 |
| anthropic | aero-primary | baseline | http_error | 0 | 0 | 7 |  |  | 0 |
