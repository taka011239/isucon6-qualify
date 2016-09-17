sudo systemctl stop isuda.python.service
cd /home/isucon/webapp
git pull origin master
sudo systemctl start isuda.python.service
