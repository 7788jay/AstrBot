import os
import inspect

# 找出模块里所有的类名
def get_classes(p_name, arg):
    classes = []
    clsmembers = inspect.getmembers(arg, inspect.isclass)
    for (name, _) in clsmembers:
        # print(name, p_name)
        if p_name.lower() == name.lower()[:-6]:
            classes.append(name)
            break
    return classes

# 获取一个文件夹下所有的模块, 文件名和文件夹名相同
def get_modules(path):
    modules = []
    for root, dirs, files in os.walk(path):
        # 获得所在目录名
        p_name = os.path.basename(root)
        print(p_name)
        for file in files:
            if file.endswith(".py") and not file.startswith("__") and p_name.lower() == file[:-3].lower():
                modules.append(file[:-3])
    return modules
    