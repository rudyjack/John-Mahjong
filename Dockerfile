FROM python:3.11
WORKDIR /mian
COPY requirements.txt
RUN pip install -r requirements.txt
COPY . /main
CMD python bot.py
