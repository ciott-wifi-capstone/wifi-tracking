# Floor plan coordinate picker

Install OpenCV and NumPy, put your approved floor-plan image in this directory as floor_plan.jpeg, and run from this directory:

~~~powershell
python -m pip install -r requirements.txt
python .\click_to_pos.py
~~~

Click the displayed image to print the mapped coordinate. Review the pixel scale and origin in click_to_pos.py when switching to another plan. The lab image is a local input.
