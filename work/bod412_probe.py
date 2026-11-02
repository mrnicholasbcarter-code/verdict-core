from verdict.classifier import classify_known

ids = [l.strip() for l in open('work/bod419-free-ids.json') if l.strip()]
unknown = [i for i in ids if classify_known(i) is None]
print('total:', len(ids), '| unknown after #912:', len(unknown))
print('sample still-unknown:', unknown[:10])
