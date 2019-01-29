#!/usr/bin/env python
# -*- coding:utf-8 -*-
import functools
from types import FunctionType  # 函数类型
from django.conf.urls import url
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.shortcuts import HttpResponse, render, redirect
from django.http import QueryDict
from django import forms
from django.db.models import Q
from stark.utils.pagination import Pagination
from django.db.models import ForeignKey, ManyToManyField


def get_choice_text(title, field):
    """
    对于Stark组件中定义列时，choice如果想要显示中文信息，调用此方法即可。
    实现一个闭包，更方便操作者获取choices字段内容
    :param title: 希望页面显示的表头
    :param field: 字段名称
    """

    def inner(obj=None, is_header=None):
        if is_header:
            return title
        method = "get_%s_display" % field
        return getattr(obj, method)()

    return inner


def get_datetime_text(title, field, time_format='%Y-%m-%d'):
    """
    前端显示datetime格式的数据,实现一个闭包，更方便操作者获取datetime字段内容
    用法与get_choice_text一致
    :param title: 希望页面显示的表头
    :param field: 字段名称
    :param time_format: 要格式化的时间格式
    :return:
    """

    def inner(obj=None, is_header=None):
        if is_header:
            return title
        datetime_value = getattr(obj, field)
        return datetime_value.strftime(time_format)  # strftime把datetime格式转成时间戳

    return inner


def get_m2m_text(title, field):
    """
    前端显示（m2m/ForeignKey反向生成）格式的数据,实现一个闭包，更方便操作者获取一对多/多对多字段内容
    用法与get_choice_text一致
    :param title: 希望页面显示的表头
    :param field: 字段名称
    :return:
    """

    def inner(obj=None, is_header=None):
        if is_header:
            return title
        queryset = getattr(obj, field).all()  # 通过反射获取该对象的字段
        text_list = [str(row) for row in queryset]
        return '，'.join(text_list)

    return inner


class SearchGroupRow(object):
    def __init__(self, title, queryset_or_tuple, option, query_dict):
        """
        让choice和foreignkey或manytomany有一个统一的样式，返回给前端直接用for循环进行渲染。即把前端用于判断的代码写到了后端
        :param title: 组合搜索的列名称
        :param queryset_or_tuple: 组合搜索关联获取到的数据
        :param option: 配置
        :param query_dict: request.GET
        """
        self.title = title
        self.queryset_or_tuple = queryset_or_tuple
        self.option = option
        self.query_dict = query_dict

    def __iter__(self):
        yield '<div class="whole">'
        yield self.title
        yield '</div>'
        yield '<div class="others">'
        total_query_dict = self.query_dict.copy()  # QueryDict内置的copy()为深拷贝，拷贝一份为了让后续其他操作不会互相影响
        total_query_dict._mutable = True

        origin_value_list = self.query_dict.getlist(self.option.field)
        if not origin_value_list:  # 如果没有字段被选中，则选中全部
            yield "<a class='active' href='?%s'>全部</a>" % total_query_dict.urlencode()
        else:  # 如果该字段被选中了
            total_query_dict.pop(self.option.field)  # 获取点击全部按钮的url
            yield "<a href='?%s'>全部</a>" % total_query_dict.urlencode()  # 当点击全部时，删除已选的字段参数

        for item in self.queryset_or_tuple:
            text = self.option.get_text(item)  # 获取文本
            value = str(self.option.get_value(item))  # 获取choice/对象对应的id
            query_dict = self.query_dict.copy()
            query_dict._mutable = True

            if not self.option.is_multi:  # 单选
                query_dict[self.option.field] = value  # 给url设置新的参数
                if value in origin_value_list:
                    query_dict.pop(self.option.field)
                    yield "<a class='active' href='?%s'>%s</a>" % (query_dict.urlencode(), text)
                else:
                    yield "<a href='?%s'>%s</a>" % (query_dict.urlencode(), text)
            else:  # 多选
                # {'gender':['1','2']}
                multi_value_list = query_dict.getlist(self.option.field)
                if value in multi_value_list:  # 如果url中已经有value
                    multi_value_list.remove(value)  # 在url中删除指定value
                    query_dict.setlist(self.option.field, multi_value_list)
                    yield "<a class='active' href='?%s'>%s</a>" % (query_dict.urlencode(), text)
                else:
                    multi_value_list.append(value)
                    query_dict.setlist(self.option.field, multi_value_list)
                    yield "<a href='?%s'>%s</a>" % (query_dict.urlencode(), text)

        yield '</div>'


class Option(object):
    def __init__(self, field, is_multi=False, db_condition=None, text_func=None, value_func=None):
        """
        :param field: 组合搜索关联的字段
        :param is_multi: 是否支持多选
        :param db_condition: 数据库关联查询时的条件
        :param text_func: 此函数用于显示组合搜索按钮页面文本
        :param value_func: 此函数用于显示组合搜索按钮值
        """
        self.field = field
        self.is_multi = is_multi
        if not db_condition:
            db_condition = {}
        self.db_condition = db_condition
        self.text_func = text_func
        self.value_func = value_func

        self.is_choice = False

    def get_db_condition(self, request, *args, **kwargs):
        return self.db_condition  # 筛选条件

    def get_queryset_or_tuple(self, model_class, request, *args, **kwargs):
        """
        根据字段去获取数据库关联的数据
        :return:
        """
        field_object = model_class._meta.get_field(self.field)
        title = field_object.verbose_name
        # 获取关联数据
        if isinstance(field_object, ForeignKey) or isinstance(field_object, ManyToManyField):
            # FK和M2M,应该去获取其关联表中的数据： QuerySet
            db_condition = self.get_db_condition(request, *args, **kwargs)
            # Django1.*  找到关联表的对象使用.rel
            # return SearchGroupRow(title, field_object.rel.model.objects.filter(**db_condition), self, request.GET)
            # Django2.*  找到关联表的对象使用.remote_field
            return SearchGroupRow(title, field_object.remote_field.model.objects.filter(**db_condition), self,
                                  request.GET)
        else:
            # 获取choice中的数据：元组
            self.is_choice = True
            return SearchGroupRow(title, field_object.choices, self, request.GET)

    def get_text(self, field_object):
        """
        获取页面显示的文本
        :param field_object:
        :return:
        """
        if self.text_func:
            return self.text_func(field_object)  # 执行自定制函数

        if self.is_choice:  # choice
            return field_object[1]

        # foreignkey or manytomany
        return str(field_object)  # 触发model的__str__

    def get_value(self, field_object):
        """
        获取字段本身的ID
        :param field_object:
        :return:
        """
        if self.value_func:
            return self.value_func(field_object)

        if self.is_choice:
            return field_object[0]

        return field_object.pk


class StarkModelForm(forms.ModelForm):
    """
    默认的ModelForm，可继承该类用于样式美化。也可以自行定制表单的样式
    """

    def __init__(self, *args, **kwargs):
        super(StarkModelForm, self).__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs['class'] = 'form-control'


class StarkHandler(object):
    list_display = []  # 页面显示字段

    per_page_count = 10  # 每页展示数据条数

    has_add_btn = True  # 是否有添加按钮

    model_form_class = None  # 自定义Form表单模型

    order_list = []  # 前端数据展示顺序

    search_list = []  # 搜索框用于的筛选字段

    action_list = []  # 下拉框执行操作

    search_group = []  # 组合搜索

    def __init__(self, site, model_class, prev):
        self.site = site  # StarkSite对象
        self.model_class = model_class
        self.prev = prev
        self.request = None  # 默认为None，目的是为了让该类所有的函数都可以使用request，而不用在调用不同的函数时，传递request参数

    def display_checkbox(self, obj=None, is_header=None):
        """
        :param obj:数据库循环时每一行的对象
        :param is_header:表头
        """
        if is_header:  # 获取表头header_list时，给函数传is_header=True
            return mark_safe('<input type="checkbox" id="checkAll">')
        return mark_safe('<input type="checkbox" name="pk" value="%s" />' % obj.pk)

    def display_edit(self, obj=None, is_header=None):
        """
        自定义页面显示的列（表头和内容）
        :param obj:
        :param is_header:表头
        """
        if is_header:
            return "编辑"
        # 根于stark生成的name进行反向生成URL
        return mark_safe(
            '<a href="%s" class="btn btn-warning btn-xs" data-toggle="tooltip" data-placement="top" title="编辑"><i class="fa fa-wrench"></i></a>' % self.reverse_change_url(
                pk=obj.pk))

    def display_del(self, obj=None, is_header=None):
        if is_header:
            return "删除"
        return mark_safe(
            '<a href="%s" class="btn btn-danger btn-xs" data-toggle="tooltip" data-placement="top" title="删除"><i class="fa fa-remove"></i></a>' % self.reverse_delete_url(
                pk=obj.pk))

    def get_list_display(self):
        """
        获取页面上应该显示的列，预留的自定义扩展，例如：以后根据用户的不同显示不同的列
        可以和权限组件进行配合使用，不同用户显示不通的列
        """
        value = []
        value.extend(self.list_display)
        return value

    def get_add_btn(self, request, *args, **kwargs):
        if self.has_add_btn:
            return "<a href='%s' class='btn btn-tumblr btnCreate' style='margin-bottom: 10px' data-toggle='tooltip' data-placement='top' title='' data-original-title='添加'><i class='fa fa fa-plus'></i> <span>添加</span></a>" % self.reverse_add_url(
                *args, **kwargs)
        return None

    def get_model_form_class(self):
        """
        根据不同的models，生成不同的modelform
        """
        if self.model_form_class:  # 支持自定制model_form_class
            return self.model_form_class

        class DynamicModelForm(StarkModelForm):
            class Meta:
                model = self.model_class
                fields = "__all__"

        return DynamicModelForm

    def get_order_list(self):
        return self.order_list or ['-id', ]

    def get_search_list(self):
        return self.search_list

    def get_action_list(self):
        return self.action_list

    def action_multi_delete(self, request, *args, **kwargs):
        """
        批量删除（如果想要定制执行成功后的返回值，那么就为action函数设置返回值即可。）
        """
        pk_list = request.POST.getlist('pk')
        self.model_class.objects.filter(id__in=pk_list).delete()

    action_multi_delete.text = "批量删除"

    def get_search_group(self):
        return self.search_group

    def get_search_group_condition(self, request):
        """
        获取组合搜索的条件
        """
        condition = {}
        # ?depart=1&gender=2&page=123&q=999
        for option in self.get_search_group():
            if option.is_multi:
                values_list = request.GET.getlist(option.field)  # tags=[1,2]
                if not values_list:
                    continue
                condition['%s__in' % option.field] = values_list
            else:
                value = request.GET.get(option.field)
                if not value:
                    continue
                condition[option.field] = value
        return condition

    def get_queryset(self, request, *args, **kwargs):
        """
        拿到当前模型全数据的queryset，可被重写覆盖进行筛选数据queryset
        """
        return self.model_class.objects

    def changelist_view(self, request, *args, **kwargs):
        """
        列表页面
        """

        # ########## 1. 处理Action ##########
        action_list = self.get_action_list()
        # func.__name__获取函数名，如果直接给前端传递func函数，在前端会自动调用func()，
        action_dict = {func.__name__: func.text for func in action_list}  # {'multi_delete':'批量删除','multi_init':'批量初始化'}

        if request.method == 'POST':
            action_func_name = request.POST.get('action')
            if action_func_name and action_func_name in action_dict:  # 确认是否在action_dict里，防止恶意
                action_response = getattr(self, action_func_name)(request, *args, **kwargs)
                if action_response:  # 如果执行的函数有返回值，例如执行后确认或执行后跳转到其他页面
                    return action_response  # 执行函数返回值

        # ########## 2. 获取搜索条件 ##########
        search_list = self.get_search_list()
        search_value = request.GET.get('q', '')
        conn = Q()
        conn.connector = 'OR'  # 构造or条件
        if search_value:
            for item in search_list:
                conn.children.append((item, search_value))
                # 构造搜索条件: name__contains="Kris"/depart="IT"。。。。

        # ########## 3. 获取排序 ##########
        order_list = self.get_order_list()
        # 获取组合的条件
        search_group_condition = self.get_search_group_condition(request)
        # 获取当前model全数据queryset
        prev_queryset = self.get_queryset(request, *args, **kwargs)
        # filter(conn) 过滤搜索对象
        queryset = prev_queryset.filter(conn).filter(**search_group_condition).order_by(
            *order_list)

        # ########## 4. 处理分页 ##########
        all_count = queryset.count()  # 获取总数据

        query_params = request.GET.copy()
        query_params._mutable = True

        pager = Pagination(
            current_page=request.GET.get('page'),
            all_count=all_count,
            base_url=request.path_info,
            query_params=query_params,
            per_page=self.per_page_count,
        )  # 实例化分页组件

        data_list = queryset[pager.start:pager.end]

        # ########## 5. 处理表格 ##########
        list_display = self.get_list_display()
        # 5.1 处理表格的表头
        header_list = []
        if list_display:
            for key_or_func in list_display:
                if isinstance(key_or_func, FunctionType):  # 如果是函数(编辑/删除/复选框....)
                    verbose_name = key_or_func(self, obj=None, is_header=True)
                else:  # 是字段则在数据库中获取
                    verbose_name = self.model_class._meta.get_field(key_or_func).verbose_name
                header_list.append(verbose_name)
        else:  # 如果没有list_display则使用该类的表名称
            header_list.append(self.model_class._meta.model_name)

        # 5.2 处理表的内容
        body_list = []
        for row in data_list:
            tr_list = []
            if list_display:
                for key_or_func in list_display:
                    if isinstance(key_or_func, FunctionType):
                        tr_list.append(key_or_func(self, row, is_header=False))
                    else:
                        tr_list.append(getattr(row, key_or_func))  # obj.gender通过反射获取每个字段的内容
            else:  # 如果没有则直接打印对象的__str__
                tr_list.append(row)
            body_list.append(tr_list)

        # ########## 6. 添加按钮 #########
        add_btn = self.get_add_btn(request, *args, **kwargs)

        # ########## 7. 组合搜索 #########
        search_group_row_list = []
        search_group = self.get_search_group()  # ['gender', 'depart']
        for option_object in search_group:
            row = option_object.get_queryset_or_tuple(self.model_class, request, *args, **kwargs)
            search_group_row_list.append(row)

        return render(
            request,
            'stark/changelist.html',
            {
                'data_list': data_list,
                'header_list': header_list,
                'body_list': body_list,
                'pager': pager,
                'add_btn': add_btn,
                'search_list': search_list,
                'search_value': search_value,
                'action_dict': action_dict,
                'search_group_row_list': search_group_row_list
            }
        )

    def save(self, request, form, is_update=False):
        """
        自定义，在使用ModelForm保存数据之前预留的钩子方法
        :param is_update: 新增add不需要在save前进行判断，修改update可能会出现某个字段单独的进行添加，可以用if is_update进行判断是add/update
        """
        # 使用示例：form.instance.depart_id = 1  # 可在子类中保存数据库之前自定义一些操作，例如给depart_id设置一个默认值1
        form.save()

    def add_view(self, request, *args, **kwargs):
        """
        添加页面
        """
        model_form_class = self.get_model_form_class()
        if request.method == 'GET':
            form = model_form_class()
            return render(request, 'stark/change.html', {'form': form})
        form = model_form_class(data=request.POST)
        if form.is_valid():
            self.save(request, form, is_update=False)  # 自定义数据保存前的一些操作
            # 在数据库保存成功后，跳转回列表页面(携带原来的参数)
            return redirect(self.reverse_list_url(*args, **kwargs))
        return render(request, 'stark/change.html', {'form': form})

    def change_view(self, request, pk, *args, **kwargs):
        """
        编辑页面
        """
        current_change_object = self.model_class.objects.filter(pk=pk).first()
        if not current_change_object:
            return HttpResponse('要修改的数据不存在，请重新选择！')

        model_form_class = self.get_model_form_class()
        if request.method == 'GET':
            form = model_form_class(instance=current_change_object)
            return render(request, 'stark/change.html', {'form': form})
        form = model_form_class(data=request.POST, instance=current_change_object)
        if form.is_valid():
            self.save(request, form, is_update=True)
            return redirect(self.reverse_list_url(*args, **kwargs))  # 非弹窗验证时使用方法

        return render(request, 'stark/change.html', {'form': form})

    def delete_view(self, request, pk, *args, **kwargs):
        """
        删除页面
        """
        origin_list_url = self.reverse_list_url()
        if request.method == 'GET':
            return render(request, 'stark/delete.html', {'cancel': origin_list_url})

        self.model_class.objects.filter(pk=pk).delete()
        return redirect(origin_list_url)

    def get_url_name(self, param):
        """
        生成URL唯一name
        生成app名和表名，判断是否有自定制url，prev
        """
        app_label, model_name = self.model_class._meta.app_label, self.model_class._meta.model_name
        if self.prev:
            return '%s_%s_%s_%s' % (app_label, model_name, self.prev, param,)
        return '%s_%s_%s' % (app_label, model_name, param,)

    @property
    def get_list_url_name(self):
        """
        获取列表页面URL的name
        """
        return self.get_url_name('list')

    @property
    def get_add_url_name(self):
        """
        获取添加页面URL的name
        """
        return self.get_url_name('add')

    @property
    def get_change_url_name(self):
        """
        获取修改页面URL的name
        """
        return self.get_url_name('change')

    @property
    def get_delete_url_name(self):
        """
        获取删除页面URL的name
        """
        return self.get_url_name('delete')

    def reverse_commons_url(self, name, *args, **kwargs):
        name = "%s:%s" % (self.site.namespace, name,)  # 生成name用于发现生成需要拼接namespace
        base_url = reverse(name, args=args, kwargs=kwargs)
        if not self.request.GET:
            add_url = base_url
        else:
            param = self.request.GET.urlencode()
            new_query_dict = QueryDict(mutable=True)
            new_query_dict['_filter'] = param
            add_url = "%s?%s" % (base_url, new_query_dict.urlencode())
        return add_url

    def reverse_add_url(self, *args, **kwargs):
        """
        生成带有原搜索条件的添加URL
        """
        return self.reverse_commons_url(self.get_add_url_name, *args, **kwargs)

    def reverse_change_url(self, *args, **kwargs):
        """
        此时的self是StarkSite对象，里面包含StarkSite的name和namespace等初始化__init__的方法
        生成带有原搜索条件的编辑URL
        """
        return self.reverse_commons_url(self.get_change_url_name, *args, **kwargs)

    def reverse_delete_url(self, *args, **kwargs):
        """
        生成带有原搜索条件的删除URL
        :param args:
        :param kwargs:
        :return:
        """
        return self.reverse_commons_url(self.get_delete_url_name, *args, **kwargs)

    def reverse_list_url(self, *args, **kwargs):
        """
        跳转回列表页面时，生成URL
        """
        return self.reverse_commons_url(self.get_list_url_name, *args, **kwargs)

    def wrapper(self, func):
        """
        相当于每一次请求进来，先执行inner函数，再执行原本的视图函数
        """

        @functools.wraps(func)
        def inner(request, *args, **kwargs):
            self.request = request  # 给self.request=None赋值成request
            return func(request, *args, **kwargs)

        return inner

    def get_urls(self):
        """
        获取默认每个model类4个URL
        """
        patterns = [
            url(r'^list/$', self.wrapper(self.changelist_view), name=self.get_list_url_name),
            url(r'^add/$', self.wrapper(self.add_view), name=self.get_add_url_name),
            url(r'^change/(?P<pk>\d+)/$', self.wrapper(self.change_view), name=self.get_change_url_name),
            url(r'^delete/(?P<pk>\d+)/$', self.wrapper(self.delete_view), name=self.get_delete_url_name),
        ]
        # 如果不需要这么多URL，则可以自定制重写该函数get_urls，覆盖父类StarkHandler

        # 如果需要更多的URL，则可以自定制函数extra_urls，添加更多的URL。新的URL返回的视图函数在自定义类中书写
        patterns.extend(self.extra_urls())
        return patterns

    def extra_urls(self):
        """
        自定制更多URL，默认为空
        """
        return []


class StarkSite(object):
    def __init__(self):
        self._registry = []
        self.app_name = 'stark'
        self.namespace = 'stark'

    def register(self, model_class, handler_class=None, prev=None):
        """
        :param model_class: 是models中的数据库表对应的类名。 models.UserInfo
        :param handler_class: 处理请求的视图函数所在的类名
        :param prev: 生成URL的前缀
        """
        if not handler_class:  # 如果不使用自定制的handler
            handler_class = StarkHandler  # 使用stark组件自带的handler
        self._registry.append(
            {'model_class': model_class, 'handler': handler_class(self, model_class, prev),
             'prev': prev})

    def get_urls(self):
        patterns = []
        for item in self._registry:
            model_class = item['model_class']
            handler = item['handler']
            prev = item['prev']
            # 获取models类名和models类所在的APP名
            app_label, model_name = model_class._meta.app_label, model_class._meta.model_name
            if prev:
                # 生成url前缀路径，分发路径到handler.get_urls()里
                patterns.append(url(r'^%s/%s/%s/' % (app_label, model_name, prev,), (handler.get_urls(), None, None)))
            else:
                patterns.append(url(r'%s/%s/' % (app_label, model_name,), (handler.get_urls(), None, None)))

        return patterns

    @property
    def urls(self):
        return self.get_urls(), self.app_name, self.namespace


site = StarkSite()
