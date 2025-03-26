from datetime import datetime, timezone, timedelta

dt1 = datetime.now()
dtutc = datetime.now(timezone.utc)
diff = timedelta(minutes=180, seconds =39)
dt2 = dt1 + diff

#print(dt1.weekday())
#print(dt1.isoweekday())
#print(dt1.day)
#print(dt1.minute)

#print(dt1.time())
#print(dt2.time())

print ("dt1 dayno, Hrs, Mins, Secs=", dt1.weekday(), dt1.hour, dt1.minute, dt1.second)
print ("dt2 dayno, Hrs, Mins, Secs=", dt2.weekday(), dt2.hour, dt2.minute, dt2.second)
print ("dtutc dayno, Hrs, Mins, Secs=", dtutc.weekday(), dtutc.hour, dtutc.minute, dtutc.second)
