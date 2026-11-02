from verdict.classifier import classify_known

ids = [l.strip() for l in open('work/bod419-free-ids.json') if l.strip()]
none_ids = [i for i in ids if classify_known(i) is None]
print('total:', len(ids), '| unknown:', len(none_ids))
print('sample unknown:', none_ids[:15])
