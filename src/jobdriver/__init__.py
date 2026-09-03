"""jobdriver：供人在会话内逐步驱动的浏览器控制层。

三个模块各管一件事：
  daemon.py   抱着浏览器不撒手，对外开 localhost 接口
  cli.py      一次性进程，发一条命令就走
  condense.py 把一页 DOM 压成一张字段表，省上下文
"""
