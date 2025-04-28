import pickle
import numpy as np
f = open('.cache/calibration/gello_ur5.pkl','rb')
data = pickle.load(f)
print(data)