from verdict.classifier import classify_known, classify

cases = [
    "please compare longcat-2.0 with a competitor",
    "the docs mention muse-spark but this is not an id",
    "vendor/longcat-20",
    "vendor/claude-3.70",
    "vendor/jamba-large-8b",
    "vendor/trinity-large-7b",
    "vendor/nemotron-3-super-8b",
]
for c in cases:
    print(repr(c), "->", classify_known(c))

# residuals unchanged: the 47 stay None
ids = [l.strip() for l in open('work/bod419-free-ids.json') if l.strip()]
unknown = [i for i in ids if classify_known(i) is None]
print("total:", len(ids), "| unknown:", len(unknown))
